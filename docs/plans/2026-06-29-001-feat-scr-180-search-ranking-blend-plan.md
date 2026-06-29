---
title: "feat: SCR-180 Search ranking — blend relevance + recency across streams"
type: feat
status: completed
date: 2026-06-29
origin: docs/brainstorms/2026-06-24-ask-your-history-search-requirements.md
---

# feat: SCR-180 Search ranking — blend relevance + recency across streams

## Summary

Replace the recency-only `SearchViewModel.rank` with a **blended relevance + recency score**, normalized so the three streams (content bm25, transcript, timeline activity) are comparable on one axis. Relevance-bearing hits (content/transcript text matches) are weighted to reliably outrank a merely-newer activity row, while recency still orders items *within* a relevance tier — and the blend degrades cleanly to pure recency for pure time/app queries that carry no free text. The scoring lives in a small pure, tunable, unit-testable core so weights can be tuned against real recordings without touching the fan-out logic.

---

## Problem Frame

`SearchViewModel.rank` (`macos/ScreenCap/Views/Search/SearchViewModel.swift:509`) sorts strictly by `anchorMs` descending, using the bm25 `score` only to break ties among same-instant hits. The consequence (origin R4 gap): a strong on-screen-text or transcript match for a free-text query sits **below** an unrelated-but-newer timeline/activity row, because timeline rows carry `score: 0` and the relevance signal never participates in cross-stream ordering. There is no real relevance blend — only chronology. For a question-driven query ("refund macro"), the most relevant moment should lead, not whatever happened most recently. (see origin: `docs/brainstorms/2026-06-24-ask-your-history-search-requirements.md`, R4 + deferred-blend note)

---

## Requirements

- R1. A free-text query orders results by a **blended relevance + recency score**, not pure chronology. *(origin R4)*
- R2. The blend is **normalized** so content bm25 relevance, transcript matches, and timeline activity are comparable on one scale.
- R3. For a free-text query, a relevant text/transcript hit ranks **above** an unrelated, newer activity row. *(origin R4 acceptance)*
- R4. Within the same relevance tier, **recency** still orders results (newer first); unanchored hits have a defined, deterministic placement.
- R5. A query with **no free text** (pure time/app) degrades to the existing recency-first ordering — no relevance signal exists to blend.
- R6. The blend weights are **tunable** (named constants with documented defaults) so they can be adjusted against real recordings without restructuring the fan-out.
- R7. Ordering for representative mixed result sets is covered by `SearchViewModelTests` (plus focused pure-core tests).

**Origin actors:** A1 (the user asking their history)
**Origin flows:** F1 (parse query → fan out to three streams → ranked pointers → timeline render → open Review)
**Origin acceptance examples:** origin R4 acceptance — "for a free-text query, relevant text/transcript hits rank above unrelated newer activity; ordering is covered by `SearchViewModelTests`."

---

## Scope Boundaries

- Not changing the daemon query verbs, wire contracts, or the `SearchService` seam — this is a pure client-side ranking change over already-fetched items.
- Not adding a `score` field to `transcript.search` / `TranscriptHit` — transcript relevance stays "matched / not matched" (a constant), since the daemon contract exposes no graded transcript score.
- Not changing fan-out, client-side time filtering, transcript→timeline correlation, coverage states, truncation, or consent logic.
- Not adding semantic / embedding re-ranking (explicitly out of scope per origin "Outside v1").
- Not exposing the blend weights as a user-facing setting — they are code constants for v1.

### Deferred to Follow-Up Work

- **Final weight values tuned against real recordings**: the plan fixes the blend *structure*, the *dominance invariant*, and sensible default weights; the exact numeric tuning is an exploratory implementation-time activity against a real `~/.screencap/recordings/` library. Only the ordering invariants are pinned by tests — not specific float values. If a settings-backed knob proves desirable, file a separate Linear ticket.

---

## Context & Research

### Relevant Code and Patterns

- `macos/ScreenCap/Views/Search/SearchViewModel.swift`
  - `rank(_:)` (line 509) — the function being replaced; current recency-first comparator with bm25 tiebreak.
  - Item construction (lines 187–232): timeline/activity → `score: 0`; content/screen → `score: hit.score` (bm25); transcript/audio → `score: 0` (**no wire score**), `anchorMs` may be `nil` (unanchored), `approximate: true`.
  - `SearchResultItem` (line 555) — `stream: Stream {screen, audio, activity}`, `anchorMs: Int?`, `score: Double`. **`score == 0` is overloaded** between activity (no relevance) and transcript (text match) — relevance must be assigned per-`stream`, never inferred from `score` alone.
- `macos/ScreenCap/Models/SearchResult.swift` — `ContentHit.score` (bm25, more-negative = better, unbounded); `TranscriptHit` has **no** score; `TimelineRow` has no score.
- `macos/ScreenCapTests/SearchViewModelTests.swift` — `FakeSearchService` seam + `makeVM`; `testAllStreamsMergeAndRankByRecency` (line 83) currently asserts the recency-only order `[3000, 2000, 1000]` = `[.activity, .screen, .audio]` — this is the OLD behavior and **will deliberately flip** under the blend.

### Institutional Learnings

- `docs/solutions/` has no existing ranking/bm25 learning — this is the first relevance-blend in the codebase; no prior pattern to mirror or contradict.
- Testing preference (project memory): *fewer, better tests*; tests must target the changed code path. → pin ordering **invariants**, not exact float scores.
- macOS test execution: build/test via XcodeGen + `XcodeBuildMCP` (see project memory `project_macos_build_test.md`); a known-flaky daemon-reconnect test is unrelated to this change.

### External References

- None gathered — bm25 min-max normalization and weighted linear blending are standard, self-contained techniques; the codebase has the full data surface and the approach is dictated by the R4 acceptance criteria.

---

## Key Technical Decisions

- **Per-stream relevance, not score-derived relevance.** Because `score == 0` means different things for activity vs transcript, relevance is assigned by `stream`: content/screen → normalized bm25; transcript/audio → a constant text-match relevance; timeline/activity → 0 (in free-text mode). Rationale: avoids the overloaded-`0` trap; matches the conceptual model (timeline is the recency/activity anchor, content+transcript carry relevance).
- **Min-max normalize bm25 within the result set.** bm25 is unbounded-negative; map best (most negative) → top of a text band and worst → band floor. Within-set normalization keeps streams comparable without a global calibration. Rationale: robust to arbitrary bm25 magnitudes; single-hit / equal-score sets handled by an explicit degenerate-bound guard.
- **Text band floor > 0 with a weight-dominance invariant.** Content maps into `[textFloor, 1.0]`; transcript sits at a fixed `transcriptRelevance` inside that band; activity = 0. Weights satisfy `relevanceWeight * textFloor > recencyWeight * 1.0`, which **guarantees R3** (any text hit outranks any activity row regardless of recency) while still letting recency + graded bm25 order *within* the text tier and recency order *within* activity. Rationale: a genuine blend (not strict lexicographic), but the acceptance-critical separation is provable from the constants.
- **Default weights:** `relevanceWeight = 0.75`, `recencyWeight = 0.25`, `textFloor = 0.5`, `transcriptRelevance = 0.6`. Check: `0.75 * 0.5 = 0.375 > 0.25 * 1.0 = 0.25` ✓. These are the tunable knobs (R6); final values tuned against real data (deferred).
- **Unanchored text hits stay in the text tier.** An unanchored transcript hit (anchor `nil`) gets `recencyNorm = 0` (oldest) but keeps its text relevance — so it still ranks **above** zero-relevance activity (consistent with R3), but **below** anchored text hits. This is a deliberate change from "unanchored sorts dead last overall." Alternative considered: keep unanchored last globally — rejected because it would push a relevant transcript match below an unrelated activity row, violating R3.
- **Deterministic total order.** Swift's `sort` is not guaranteed stable; the comparator ends with a stable tiebreak on `id` so equal-blend items order deterministically (required for testable ordering).
- **Pure, tunable core in its own file.** Extract the scoring into an internal, side-effect-free helper (`macos/ScreenCap/Views/Search/SearchRanking.swift`) that takes the item list and weights and returns the ordered list. Rationale: directly unit-testable (normalization edges, dominance invariant) without driving the whole async fan-out; keeps `SearchViewModel` focused; tuning = editing constants in one place.

---

## Open Questions

### Resolved During Planning

- *Blend vs strict lexicographic?* — Genuine weighted blend, but with a weight-dominance invariant that guarantees the R3 separation. Recency still matters within tiers.
- *How does a text match beat a recent activity row (R3)?* — Resolved by the `relevanceWeight * textFloor > recencyWeight` invariant, not by hoping the weights happen to work.
- *Transcript relevance with no wire score?* — Fixed constant inside the text band; transcript is "matched text," graded relevance unavailable by daemon contract.
- *Pure time/app queries?* — All items are activity (relevance 0) → blend collapses to recency-only; existing behavior preserved (R5).

### Deferred to Implementation

- **Exact weight/floor values** — structure + defaults + invariant are fixed; final tuning is exploratory against a real recordings library (see Deferred to Follow-Up Work). Tests assert invariants, not floats.
- **Whether to log/inspect computed blended scores during tuning** — a temporary dev aid at most; not part of the shipped surface.

---

## High-Level Technical Design

> *This illustrates the intended approach and is directional guidance for review, not implementation specification. The implementing agent should treat it as context, not code to reproduce.*

```
rank(items):
    # 1. set-level normalization bounds (guard degenerate min==max)
    bm25 = [it.score for it in items if it.stream == .screen]
    relBounds = minMax(bm25)                 # nil when no content hits
    anchors = [it.anchorMs for it in items if it.anchorMs != nil]
    recBounds = minMax(anchors)              # nil when nothing anchored

    # 2. per-item blended score
    for it in items:
        relevance =
            it.stream == .screen   -> mapBm25(it.score, relBounds) into [textFloor, 1.0]
            it.stream == .audio    -> transcriptRelevance            # constant text band
            it.stream == .activity -> 0.0
        recency =
            it.anchorMs == nil     -> 0.0                            # unanchored = oldest
            else                   -> normalize(it.anchorMs, recBounds)   # [0,1], newest=1
        it.blended = relevanceWeight * relevance + recencyWeight * recency

    # 3. deterministic descending order
    sort items by (blended desc, anchorMs desc, score asc, id asc)

# Invariant that guarantees R3:
#   relevanceWeight * textFloor  >  recencyWeight * 1.0
#   => any text hit (relevance >= textFloor) outranks any activity row (relevance 0),
#      regardless of recency.
```

Degenerate-bounds rule: when a normalization band has `min == max` (single hit, or all-equal), map every member to the **top** of its band (relevance) / treat recency as equal (so the lower-priority tiebreakers decide). This keeps a lone strong content hit at full text relevance rather than collapsing to 0.

---

## Implementation Units

### U1. Pure blended-ranking core

**Goal:** A side-effect-free, tunable scoring core that takes `[SearchResultItem]` + weight constants and returns the items in blended relevance+recency order, with the dominance invariant and degenerate-bounds handling baked in.

**Requirements:** R1, R2, R3, R4, R6

**Dependencies:** None

**Files:**
- Create: `macos/ScreenCap/Views/Search/SearchRanking.swift`
- Create: `macos/ScreenCapTests/SearchRankingTests.swift`
- Modify: `macos/ScreenCap/project.yml` or the XcodeGen sources config **only if** new files under existing source roots are not auto-globbed (verify first; most likely no change needed)

**Approach:**
- Define an internal `BlendWeights` struct (or static constants) with documented defaults: `relevanceWeight = 0.75`, `recencyWeight = 0.25`, `textFloor = 0.5`, `transcriptRelevance = 0.6`. Include a comment stating the dominance invariant `relevanceWeight * textFloor > recencyWeight`.
- Internal function `rankBlended(_ items: [SearchResultItem], weights: BlendWeights = .default) -> [SearchResultItem]`.
- Relevance is assigned **per `stream`** (screen → normalized bm25 into `[textFloor, 1.0]`; audio → `transcriptRelevance`; activity → `0`), never inferred from `score` alone.
- Min-max normalization helpers with explicit `min == max` (and empty) guards mapping to the band top / equal recency.
- Final comparator is a deterministic total order: `(blended desc, anchorMs desc nils-last, score asc, id asc)`.

**Technical design:** see High-Level Technical Design above — same structure, scoped to this pure function. Directional, not implementation spec.

**Patterns to follow:**
- `SearchResultItem` shape and the existing `rank` comparator's nil-handling in `macos/ScreenCap/Views/Search/SearchViewModel.swift:509`.
- Existing Swift test style in `macos/ScreenCapTests/SearchViewModelTests.swift` (XCTest, `@testable import ScreenCap`, plain struct fixtures).

**Test scenarios:**
- Happy path: a content hit (bm25 `-2.0`) + a newer activity row → content ranks first (Covers R3 / origin R4 acceptance).
- Happy path: two content hits, stronger bm25 (more negative) ranks above weaker, both above activity.
- Edge case: single content hit (min == max bm25) → maps to top of text band, ranks above activity, no divide-by-zero.
- Edge case: transcript hit + newer activity row → transcript (text band) ranks above activity (Covers R3).
- Edge case: anchored vs unanchored transcript hit → anchored ranks above unanchored; **both** rank above a newer activity row.
- Edge case: all-activity set (no free-text relevance) → pure recency order, newest first (Covers R5).
- Edge case: empty item list → returns empty; single-item list → returns that item.
- Determinism: two items with identical blended score + anchor + score → deterministic order by `id` (run twice, identical output).
- Invariant: with default weights, a text hit at `textFloor` relevance and recency 0 still outranks an activity row at recency 1.0.

**Verification:** `SearchRankingTests` pass; the dominance invariant test passes with the shipped default weights; no float-equality assertions on blended scores (only ordering).

---

### U2. Wire `SearchViewModel.rank` to the blend and update view-model ordering tests

**Goal:** Replace the body of `SearchViewModel.rank` with a call into the U1 core, and update/extend `SearchViewModelTests` so the end-to-end fan-out produces the blended order — including the deliberate flip of the existing recency-only assertion.

**Requirements:** R1, R3, R4, R5, R7

**Dependencies:** U1

**Files:**
- Modify: `macos/ScreenCap/Views/Search/SearchViewModel.swift` (`rank(_:)` at line 509; update its doc comment from "Recency-first" to the blend description)
- Modify: `macos/ScreenCapTests/SearchViewModelTests.swift`

**Approach:**
- `rank` becomes a thin delegate to `rankBlended(_:weights:)` from U1 — no scoring logic left in the view model.
- Keep all other `search()` behavior untouched (fan-out, time filter, correlation, coverage, truncation, consent, cancellation guards).
- Update `testAllStreamsMergeAndRankByRecency` (line 83): under the blend, the content hit (bm25 `-1.2`, a real text match) and the transcript hit outrank the newer activity row. Rename it (e.g. `testAllStreamsMergeAndRankByRelevanceThenRecency`) and assert the new order: `[.screen, .audio, .activity]` (content first, transcript text-match second, activity last) — documenting the intended R4 behavior change in a comment.
- Add a focused end-to-end ordering test for an unrelated-newer-activity scenario driven through `vm.search()` against the `FakeSearchService`.

**Patterns to follow:**
- `FakeSearchService` + `makeVM` + `loaded(_:)` helpers already in `macos/ScreenCapTests/SearchViewModelTests.swift`.
- Time-filter and correlation tests remain valid as-is — confirm they still pass (only ordering semantics changed, not membership/filtering).

**Test scenarios:**
- Happy path (flip of existing test): timeline @3000 (activity) + content @2000 (bm25 `-1.2`) + transcript chunk0→1000 → order `[2000 .screen, 1000 .audio, 3000 .activity]` (Covers origin R4 acceptance).
- Happy path: free-text query where a relevant content hit is the **oldest** item but still ranks first over a newer unrelated activity row (Covers R3).
- Edge case: pure time/app query ("today") with only activity rows → recency-first order unchanged (Covers R5) — extend/keep `testNoConsentPromptForPureTimeOrAppQuery`-style fixture or add a dedicated ordering assertion.
- Regression: `testTimeWindowFiltersContentClientSide`, `testTranscriptCorrelationSnapsToNearestTimelineEvent`, `testTranscriptUnanchoredWhenRecordingHasNoTimeline`, `testPartialContentErrorStillReturnsTimeline` still pass (membership/coverage/anchoring unchanged).

**Verification:** full `SearchViewModelTests` suite passes; the flipped test documents and asserts the new relevance-first order; no other `search()` behavior regressed.

---

## System-Wide Impact

- **Interaction graph:** `rank` is called once from `search()` (line 238) and feeds `SearchResults.items`, consumed by `SearchResultsView` / `SearchTimelineView` / keyboard nav. Only *ordering* changes; item identity, count, anchors, and snippets are unchanged, so downstream views, deep-linking (`SearchSeek`), and highlighting are unaffected.
- **Error propagation:** none — pure in-memory reordering; no new failure modes, no new awaits.
- **State lifecycle risks:** none — no persistence, no caching; `rank` is recomputed per search.
- **API surface parity:** the daemon-side MCP/CLI search ranks independently (Python side); this change is macOS-app-only and does not alter wire contracts. No parity obligation in this ticket.
- **Integration coverage:** end-to-end ordering proven through `vm.search()` against `FakeSearchService` (U2); pure-core invariants proven directly (U1).
- **Unchanged invariants:** fan-out independence, client-side time filtering, transcript→timeline correlation, per-stream coverage states, truncation flag, consent trigger, and the SCR-182 latest-wins cancellation guards all remain exactly as-is.

---

## Risks & Dependencies

| Risk | Mitigation |
|------|------------|
| Weights chosen badly → text *always* buries recent activity even when the user wanted recency | Dominance invariant only guarantees text-over-activity for free-text queries; pure time/app queries stay recency-only (R5). Defaults tuned against real recordings (deferred); invariant tests lock the acceptance, not the feel. |
| Existing tests encode old recency-only order and break | Expected and intended — U2 explicitly flips `testAllStreamsMergeAndRankByRecency` and documents why; other tests verified unaffected. |
| Overloaded `score == 0` causes transcript to be treated as zero-relevance | Relevance assigned per-`stream`, never from `score` alone — called out as a Key Technical Decision and covered by a transcript-vs-activity test. |
| Non-deterministic ordering from unstable `sort` flakes tests | Comparator is a deterministic total order ending in an `id` tiebreak; a determinism test asserts stable output. |
| New files not picked up by XcodeGen | Verify source globbing before assuming a `project.yml` edit; add to sources only if not auto-included. |

---

## Sources & References

- **Origin document:** [docs/brainstorms/2026-06-24-ask-your-history-search-requirements.md](docs/brainstorms/2026-06-24-ask-your-history-search-requirements.md) — R4 (relevance+recency ranking) + deferred-blend open question.
- Parent plan (blend deferred to implementation): [docs/plans/2026-06-24-002-feat-ask-your-history-search-plan.md](docs/plans/2026-06-24-002-feat-ask-your-history-search-plan.md) (U4, line 144).
- Code under change: [macos/ScreenCap/Views/Search/SearchViewModel.swift](macos/ScreenCap/Views/Search/SearchViewModel.swift) (`rank`, line 509); [macos/ScreenCap/Models/SearchResult.swift](macos/ScreenCap/Models/SearchResult.swift); [macos/ScreenCapTests/SearchViewModelTests.swift](macos/ScreenCapTests/SearchViewModelTests.swift).
- Linear: [SCR-180](https://linear.app/zk-email/issue/SCR-180/search-ranking-blend-relevance-recency-across-streams) (related: [SCR-174](https://linear.app/zk-email/issue/SCR-174/ask-your-history-search-in-app-v1)).
