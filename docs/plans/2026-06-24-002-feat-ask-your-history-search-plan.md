---
title: "feat: Ask-Your-History Search (in-app, v1)"
type: feat
status: active
date: 2026-06-24
origin: docs/brainstorms/2026-06-24-ask-your-history-search-requirements.md
---

# feat: Ask-Your-History Search (in-app, v1)

## Summary

Add a **Search** section to the existing singleton main window (a 4th `NavigationSplitView` item in `macos/Screencap/Views/MainWindow.swift`, mirroring `RecordingsListView`), backed by three new `DaemonClient` methods + pointer-only Codable models over the existing UNIX-socket transport. A local rule-based parser turns the query into time/app/text filters; results fan out across the three existing daemon verbs, are time-filtered client-side where the verbs can't, ranked by relevance+recency, rendered as markers on a scrubbable per-day timeline, and deep-linked into the native Review window via a new seek-on-open entry point. One small Python-side change exposes the OCR-indexing flag through `settings` so a one-time consent prompt can enable it; everything else is additive Swift.

---

## Problem Frame

Screencap's SwiftUI app has no way for a human to search their own recorded history — the sidebar is Calendar / Recordings / Privacy, and the powerful local retrieval verbs (`/v0/content.search`, `/v0/transcript.search`, `/v0/timeline.query`) are reachable only by agents over MCP. A non-technical operator who remembers "I saw that error in the vendor portal yesterday afternoon" can only scroll a date-grouped list. (Full motivation in origin: docs/brainstorms/2026-06-24-ask-your-history-search-requirements.md.)

---

## Requirements

**Search surface & input**
- R1. In-app Search section alongside Calendar / Recordings / Privacy; a single free-text input searching all three local streams at once. *(origin R1)*
- R2. Natural phrasing interpreted by **local, rule-based parsing** of time expressions, app/site names, and free-text terms — no ML, no network. *(origin R2)*

**Retrieval & ranking**
- R3. Retrieval runs entirely against the local index via the existing on-device verbs; results are **pointers**, never synthesized prose. *(origin R3)*
- R4. Results ranked by **relevance + recency**, not pure chronological. *(origin R4)*

**Results & navigation**
- R5. Results render as **markers on a scrubbable per-day timeline** with date navigation; selecting one opens the native Review window at that exact timestamp. *(origin R5)*
- R6. Each result is recognizable/verifiable: app/window, timestamp, thumbnail/snippet with matched text highlighted. *(origin R6)*

**Privacy & trust**
- R7. Visibly local-only ("searches only what's on this Mac"); no query/result/content leaves the device. *(origin R7)*
- R8. Results stay pointer-based; no new persistent stores of raw captured text. *(origin R8)*

**Coverage & honest states**
- R9. Honest coverage states: distinguish "no matches" vs "still indexing / not yet indexed" vs per-stream coverage differences (timeline authoritative; content/transcript best-effort). *(origin R9)*
- R10. Because on-screen-text (OCR) indexing is off by default, present a **one-time consent prompt** on the first text search that would benefit; enable indexing only on consent, never silently. *(origin R10)*

**Origin actors:** A1 (Operator — non-technical user searching own history), A2 (Local retrieval layer — read-only on-device daemon query path + index)
**Origin flows:** F1 (Ask and jump to a moment), F2 (Coverage gap / nothing useful)
**Origin acceptance examples:** AE1 (covers R2, R5), AE2 (covers R3, R6), AE3 (covers R9), AE4 (covers R10)

---

## Scope Boundaries

### Deferred for later

*(Carried from origin — the destination, sequenced after v1; do not foreclose.)*
- Global hotkey / Spotlight-style overlay.
- One small on-device model as the intent brain (intent→filters only), replacing the rule-based parser.
- Optional, always-cited "summarize this period" reusing that model.
- Semantic / embedding re-ranking.

### Outside this product's identity

*(Carried from origin — positioning rejections; the plan must not build these.)*
- Any cloud LLM or off-device processing of queries, results, or content.
- Generated prose answers / synthesized claims about history (pointer results only).
- Changes to capture or the privacy/redaction pipeline (read-only surfacing feature).
- Multi-day "reconstruct my week" narrative cards.
- `browser_url`-based search/display (the `timeline.query` verb omits it in v1).

### Deferred to Follow-Up Work

*(Plan-local implementation sequencing.)*
- **OCR backfill of existing recordings**: a one-shot re-OCR/index job over historical recordings so pre-consent screen text becomes searchable. v1 is forward-only (consent indexes newly-recorded screens); separate follow-up.
- **New native Review playback**: this plan only adds a seek-on-open entry point and depends on the in-flight v1.1 native playback as the target; building/finishing playback is separate.

---

## Context & Research

### Relevant Code and Patterns

- **Sidebar wiring** — `macos/Screencap/Views/MainWindow.swift`: `SidebarSection` enum + `List(selection:)` + `sectionContent` switch. Add a `.search` case (enum + `NavigationLink` row + switch arm). Confirm the `.onChange(of: section)` `selectedDate`-clear logic doesn't need to include `.search`.
- **View template** — `macos/Screencap/Views/RecordingsListView.swift`: `@EnvironmentObject`/`@StateObject` data source, loading/empty/error branches, `Button { } label: { HStack }` rows with `.contentShape(Rectangle())`, `@Environment(\.openWindow)` for navigation, `@State rowError` + `.alert`.
- **Daemon client** — `macos/Screencap/Controllers/DaemonClient.swift`: generic `request<T: Decodable>(method:path:body:timeout:)` over `NWConnection(.unix)`, per-verb static methods, `Encodable` request structs, `Decodable` responses with snake→camel `CodingKeys`, `DaemonClientError` (incl. `envelopeError(code:rawBody:)`, `socketUnavailable`). Mirror `recordingStart` for the three new POST verbs.
- **Service seam** — `macos/Screencap/Controllers/DaemonSessionService.swift` (`LiveDaemonSessionService`): protocol seam wrapping `DaemonClient` for `@MainActor` testability. Mirror as `SearchService`.
- **Index/caching** — `macos/Screencap/State/RecordingsIndex.swift`: on-demand `refresh()` with `guard !isLoading` re-entrancy guard, no timers; `groupedByDay()`/`countsByDay` for which days have recordings (anchors the per-day timeline).
- **Review window** — `macos/Screencap/Views/Review/ReviewWindow.swift`, `ReviewWindowViewModel.swift`, `State/ReviewWindowOpener.swift`, `Views/Review/TimelinePane.swift`: scene keyed on recording-name `String`; internal `seek(toSeconds:)` exists; absolute→relative conversion via `max(0, t - startedAt)`; `.reviewWindowUploadSucceeded` NotificationCenter pattern is the out-of-band-signal precedent.
- **Consent/config** — `macos/Screencap/Controllers/PrivacyController.swift` (`JSONInvoker` seam, optimistic `@Published` update + re-fetch, `pendingToggles` double-tap guard, `hasPrivacySection` never-written-vs-default detection), `Views/Privacy/FirstRunPrivacyBanner.swift` (non-blocking banner pattern), `Models/PrivacyStatus.swift`.
- **Models** — `macos/Screencap/Models/RecordingSummary.swift` / `UploadEventLine.swift`: `Decodable` + explicit `CodingKeys`, `decodeIfPresent ?? default` tolerance, colocated domain logic.
- **Verb contract** — `src/screencap/daemon/app.py` (`content_search`/`transcript_search`/`timeline_query`), `src/screencap/daemon/schema.py` (request/response models), `src/screencap/content_index.py` (`IndexState`). MCP pessimistic defaulting precedent: `src/screencap/mcp/server.py`.
- **OCR-flag config** — `src/screencap/config.py` `get_content_index_enabled()` (top-level `content_index_enabled`, env-or-config, default off); CLI `settings` command in `src/screencap/cli/__init__.py` (`_BOOL_KEYS` allowlist, `settings_payload`, `invalidate_config_cache()`). Consumed at recording time by `chunk_processor._index_chunk_content`.

### Institutional Learnings

- **`Window` not `WindowGroup` for singletons** — `docs/solutions/ui-bugs/swiftui-windowgroup-vs-window-singleton-scene-2026-05-18.md`. Search is a section inside the existing singleton main `Window`; the Review window stays keyed on recording-name (don't add `seekToMs` to the scene value — that spawns duplicate windows).
- **Review-data timing fields are nullable** — `docs/solutions/integration-issues/review-data-nullable-timing-swift-consumer-2026-06-01.md`. Gate readiness on `ok` + paths only; the deep-link seek must guard a null/0 `startedAt` (fall back to seek=0).
- **Typed error before spawn** — `docs/solutions/integration-issues/daemon-start-failure-typed-error-before-spawn-2026-06-08.md`. Surface search failures via the existing `envelopeError(code:rawBody:)` seam; `timeline.query`'s `invalid_range` is a typed 400 the client must handle.
- **Foundation.Process / pipe pitfalls** — `docs/solutions/integration-issues/macos-foundation-process-pipe-pitfalls.md`. Search uses the UDS `DaemonClient`, NOT subprocess pipes — these pitfalls do not apply to the search path. Do not add a CLI fallback (the verbs have none).
- **No timer fork-bombs** — `RecordingsIndex` is on-demand by design; Search is user-initiated (type → submit/debounce), so no background polling loop.
- **XcodeGen stale project** — `docs/solutions/build-errors/xcodegen-stale-project-missing-new-sources.md`. After adding new Swift files, run `cd macos && xcodegen generate` before trusting any "cannot find … in scope" error.

### External References

- None — local patterns are sufficient; no high-risk external domain. (Decision recorded in Key Technical Decisions.)

---

## Key Technical Decisions

- **Search is a section in the singleton main `Window`, not a new scene.** Inherits singleton behavior; only `MainWindow.swift` sidebar wiring changes. *(learning: Window-vs-WindowGroup)*
- **Deep-link seek delivered out-of-band; Review window stays keyed on recording-name.** Adding `seekToMs` to the `WindowGroup(for:)` value would spawn duplicate windows for the same recording at different timestamps. Instead post a seek request (NotificationCenter, mirroring `.reviewWindowUploadSucceeded`) and apply it after the window reports `.ready`, guarding a null `startedAt`.
- **Daemon-only transport, no CLI fallback.** The three verbs are daemon-only; on `socketUnavailable`/`connectionFailed`, Search shows a "daemon not running" state reusing existing transport-failure UI — no search-specific auto-spawn (auto-spawn is CLI-only and these verbs aren't in `_ACTIVITY_PATHS`).
- **Time × free-text filtering is split.** `timeline.query` is time-filtered server-side (`start_ms`/`end_ms`) — pass an explicit `limit=200` because the verb defaults to 50 and truncates **earliest-first** by `timestamp_ms`, so without a raised limit + bounding window, recency ranking would silently drop the most recent hits. `content.search`/`transcript.search` can't time-filter server-side: filter **content** hits client-side by their `timestamp_ms`; **transcript** hits carry no `timestamp_ms` (see below), so filter/anchor them by their resolved chunk-start time.
- **Coverage honesty is data-driven.** Map `content.search`'s `index_state` (`ok`/`no_match`/`not_indexed`/`index_degraded`/`store_unavailable`) and each verb's `coverage` (`authoritative`/`best_effort`) to distinct user-facing states; default pessimistically (unknown → "no data," never silently "no match"), mirroring `mcp/server.py`. Authoritative-empty ("nothing recorded then") ≠ best-effort-empty ≠ not-indexed (consent CTA). **`index_state` is returned by `content.search` only** — `transcript.search`/`timeline.query` return `coverage` only, so pessimistic defaulting must NOT demote a transcript/timeline stream that returned hits just because it has no `index_state`.
- **Transcript hits anchor at a resolved chunk-start time, labeled approximate.** `TranscriptHit` carries only `chunk_index` — **no `timestamp_ms`** (the fine timestamps live in an R7-leak JSON the daemon refuses to read). A transcript hit therefore cannot be client-time-filtered or placed on the day timeline directly; v1 must **resolve `chunk_index` → an absolute chunk-start time** (correlate the recording's chunk window via `timeline.query`/recording metadata), then filter/place by that time, labeled approximate. Transcript hits with no resolvable chunk time surface unanchored (not dropped, not placed on the timeline).
- **OCR-flag write surface added to `settings`, kept top-level.** `content_index_enabled` is added to the CLI `settings --set` allowlist and `settings --json` payload at its existing top-level location (no behavior change for existing configs); the consent flow flips it via the `PrivacyController` invoker pattern. Takes effect on the next recording's chunk processing (no daemon restart).
- **Consent is a separate tri-state** (`never-asked`/`consented`/`declined`), distinct from the enable flag, so "declined" never re-prompts and isn't confused with "feature off." **The trigger gates on the actual `content_index_enabled` flag being off** (read via `settings --json`), NOT on `index_state == not_indexed` alone: the daemon returns `not_indexed` only when the index DB file is absent, so once any recording has ever been indexed a flag-off user gets `no_match` and a `not_indexed`-only trigger would never fire (the silent-thin-results failure R10 exists to prevent). Fire when free-text is present AND the flag is off AND consent state is `never-asked`. (Where the tri-state persists, and whether a direct CLI `settings --set content_index_enabled=true` should reconcile the prompt state, is an open decision — see Open Questions.)
- **No external research.** Strong local patterns for every surface; no auth/payments/migration risk.

---

## Open Questions

### Resolved During Planning

- *Backfill of existing recordings on consent?* — No. v1 is forward-only; backfill is Deferred to Follow-Up Work. Consent copy scoped to "newly recorded screens become searchable."
- *Enable mechanism for OCR indexing?* — Add a `settings` read/write surface (U1); flip via the existing config-invoker pattern.
- *Daemon unreachable on Search load?* — Reuse the app's existing transport-failure/daemon state; no new auto-spawn.
- *Per-day timeline for multi-day result sets?* — A day strip; default to the most-recent day containing a hit.
- *Dangling pointer (recording evicted between index and click)?* — Graceful "recording no longer available" terminal state + drop the stale marker.
- *Consent trigger condition?* — Gate on the `content_index_enabled` flag being off (read via `settings --json`), not `index_state == not_indexed` alone (which only signals an absent index DB). **[resolves doc-review P1]**
- *Transcript timeline placement / time-filtering?* — `TranscriptHit` has no `timestamp_ms`; resolve `chunk_index`→chunk-start time, then filter/anchor; unresolvable hits surface unanchored. **[resolves doc-review P1]**
- *Timeline truncation vs recency?* — `timeline.query` defaults to `limit=50` and truncates earliest-first; pass `limit=200` and bound by the parsed window. **[resolves doc-review P2]**
- *Which verbs carry `index_state`?* — Only `content.search`; transcript/timeline are `coverage`-only, so pessimistic defaulting must not demote streams that returned hits. **[resolves doc-review P2]**

### Deferred to Implementation

- **Consent-state persistence + CLI bypass** [User decision] — where the consent tri-state lives (config via `settings`, visible to both app and CLI, vs app-local `UserDefaults`, lost on uninstall) and whether enabling `content_index_enabled` directly via `screencap settings --set` (a same-EUID CLI path) is reconciled on next launch or accepted as an out-of-band residual consistent with `SECURITY.md`'s same-EUID trust boundary. Scope-guardian alternative: replace the standalone `ContentIndexConsentController` with a second `settings` bool (e.g. `content_index_consent_declined`). Affects U7.
- **AE3 "still indexing" granularity** — `index_state` is global, so a per-recording "still indexing" state is not derivable from the verb; v1 surfaces a single global on-screen-text state. Revisit only if a per-recording signal is added to the backend.
- **`SearchService` protocol vs closure seam** [Technical] — keep the `DaemonSessionService`-style protocol (an established repo norm) or use a lighter `JSONInvoker`-style closure for the view-model test seam. Affects U2/U4.
- **U6 v1.1-playback dependency** [Technical] — gate U6 on the in-flight native playback being seek-capable, or define a visible "opened at start — jump-to-moment coming soon" fallback so the seek never silently no-ops (and soften the U5 result-card copy accordingly).

- **Parser breadth** — which time expressions + app-name normalizations to support in v1 (pin with table-driven tests; start with the common set: today/yesterday/this morning/afternoon/last week/last <weekday> + day ranges). Local TZ must match how `window_event.timestamp` is written.
- **Relevance+recency blend** — the exact scoring that merges an authoritative stream (timeline) with best-effort streams (content bm25 / transcript). Tune during implementation against real recordings.
- **Per-day timeline rendering performance** — marker density and the day-strip interaction at volume (200-result cap per stream); surface a "showing first N" note when a stream hits its cap.
- **Debounce / min query length** — require ≥1 non-whitespace token, no-op on empty submit; exact debounce interval tuned in implementation.

---

## High-Level Technical Design

> *This illustrates the intended approach and is directional guidance for review, not implementation specification. The implementing agent should treat it as context, not code to reproduce.*

```
query string
   │
   ▼
[QueryParser]  ── local, rule-based ──▶  { timeWindow?, appFilter?, freeText? }
   │
   ▼
[SearchViewModel] fan-out (3 independent calls; each fails soft):
   ├─ timeline.query(start_ms,end_ms,app,limit=200)  → authoritative rows (server time-filtered; earliest-first truncation)
   ├─ content.search(freeText)                       → hits + index_state  (client time-filter by their timestamp_ms)
   └─ transcript.search(freeText)                    → hits (chunk_index only — NO timestamp_ms;
                                                          resolve chunk_index→chunk-start time, then filter/anchor)
   │
   ▼
merge + rank (relevance + recency)  +  coverage state per stream
   │
   ▼
[SearchView] per-day timeline (day strip → markers)  +  honest coverage/empty/not-indexed states
   │  select marker
   ▼
post seek-request notification → open Review (by name) → on .ready, seek to (timestamp_ms → relative s, guard null startedAt)
                                                        └─ recording gone → "no longer available" + drop marker
```

Consent sub-flow: free-text present AND `content_index_enabled` flag is OFF (read via `settings --json`) AND consent tri-state == `never-asked` → one-time prompt (local-only reassurance copy) → consent writes `content_index_enabled=true` via `settings`; decline persists `declined` (timeline+transcript only thereafter, no re-prompt).

---

## Implementation Units

### U1. Backend: expose `content_index_enabled` through `settings`

**Goal:** Give the app a read/write surface for the OCR-indexing flag so the consent flow can enable it and read its state.

**Requirements:** R10

**Dependencies:** None

**Files:**
- Modify: `src/screencap/cli/__init__.py` (add `content_index_enabled` to the `settings --set` `_BOOL_KEYS` allowlist; include it in the `settings --json` `settings_payload`; ensure `invalidate_config_cache()` fires on write)
- Test: `tests/test_cli.py` (or the existing settings-command test module)

**Approach:**
- Keep the key top-level where `config.get_content_index_enabled()` already reads it (no migration; no behavior change for existing configs).
- Write path: `settings --set content_index_enabled=true --json`; read path: the key appears in `settings --json` output.
- No daemon restart needed — `chunk_processor._index_chunk_content` reads the flag fresh per chunk after cache invalidation.
- The matching Swift-side read field (`SettingsEnvelope.Inner.content_index_enabled`) is added in U7 so the consent controller can read this flag's current state (the consent trigger depends on it).

**Patterns to follow:** existing `_BOOL_KEYS` entries (`show_on_website`, `audio_default`) and `settings_payload` construction in `src/screencap/cli/__init__.py`.

**Test scenarios:**
- Happy path: `settings --set content_index_enabled=true` then `settings --json` reflects `true`; set back to `false` reflects `false`.
- Covers AE4. Edge: setting the key invalidates the config cache so a subsequent `get_content_index_enabled()` returns the new value.
- Error path: a non-bool value for the key is rejected with the same validation error as other `_BOOL_KEYS`.

**Verification:** `settings --json` includes `content_index_enabled`; `--set` toggles it; existing settings tests still pass.

---

### U2. Swift: search client methods, result models, and service seam

**Goal:** Add the three daemon verb calls, pointer-only Codable result models, and a testable `SearchService` seam.

**Requirements:** R3, R8

**Dependencies:** None

**Files:**
- Modify: `macos/Screencap/Controllers/DaemonClient.swift` (three POST methods: content.search, transcript.search, timeline.query; request + response structs)
- Create: `macos/Screencap/Models/SearchResult.swift` (pointer models: content hit `{recording, timestampMs, snippet, score}`, transcript hit `{recording, chunkIndex, snippet}`, timeline row `{recording, timestampMs, app?, title?}`; response envelopes carrying hits/rows + `indexState`/`coverage`)
- Create: `macos/Screencap/Controllers/SearchService.swift` (protocol + `LiveSearchService` wrapping `DaemonClient`)
- Test: `macos/ScreencapTests/SearchServiceTests.swift`

**Approach:**
- Mirror `recordingStart` for request encoding and `ListResponse`/`SessionSnapshotResponse` for decoding (snake→camel `CodingKeys`, tolerant optionals). Keep everything `Sendable`-clean under strict concurrency; carry raw `Data` rather than `[String:Any]`.
- The `timeline.query` method passes an explicit `limit` (200): the verb defaults to 50 and truncates **earliest-first** by `timestamp_ms`. Decode `index_state` only on the content-search response (transcript/timeline have `coverage` only). Handle the `invalid_range` typed envelope error.
- Models are pointer-only by design — no media path / image bytes (R8). `TranscriptHit` has `{recording, chunkIndex, snippet}` — note NO `timestampMs` (drives U4's chunk-resolution step).
- `SearchService` protocol lets the view-model be tested against a fake without a live socket (mirror `DaemonSessionService`). *(If the team prefers, a closure-injection seam like `PrivacyController.JSONInvoker` is an acceptable lighter alternative — see Open Questions.)*

**Patterns to follow:** `DaemonClient.recordingStart`, `Models/RecordingSummary.swift`, `Controllers/DaemonSessionService.swift`.

**Test scenarios:**
- Happy path: decode a representative envelope for each verb into its model (fields mapped, optionals tolerated).
- Edge: `timeline.query` `invalid_range` envelope decodes to the typed `envelopeError(code:)` path, not a crash.
- Edge: missing/extra wire fields don't break decoding (drift tolerance).
- Error path: `socketUnavailable` surfaces as a distinct, recognizable error (not a decode failure).

**Verification:** unit tests decode each verb's response and the typed error via a fake transport; no live socket required.

---

### U3. Swift: local query parser

**Goal:** Turn a natural-phrasing query into `{ timeWindow?, appFilter?, freeText? }` filters, fully locally.

**Requirements:** R2

**Dependencies:** None

**Files:**
- Create: `macos/Screencap/Controllers/QueryParser.swift`
- Test: `macos/ScreencapTests/QueryParserTests.swift`

**Approach:**
- Rule-based extraction of time expressions and app/site tokens; remaining text is the free-text term. No ML, no network.
- Resolve relative time to an absolute `[startMs, endMs]` in local TZ matching the recorder's clock. Surface the resolved range so the UI can show the interpretation (U5).
- Unrecognized app names fall through to free-text rather than failing.

**Patterns to follow:** plain Swift value type + pure functions; injected "now" for deterministic tests.

**Test scenarios:**
- Covers AE1. Happy path: "vendor portal yesterday afternoon" → app/free-text + a yesterday-afternoon `[startMs,endMs]` for a fixed injected now.
- Edge: pure free text (no time/app) → only `freeText`, no window.
- Edge: "last Tuesday" near a week boundary resolves to the correct prior-week range (table-driven, both sides of the boundary).
- Edge: empty / whitespace-only query → no filters (caller no-ops).
- Edge: unrecognized app token stays in free-text.

**Verification:** table-driven tests assert absolute ranges for a fixed now; parser is pure/deterministic.

---

### U4. Swift: SearchViewModel — fan-out, filter, rank, coverage

**Goal:** Orchestrate the three verbs, apply client-side time filtering, merge+rank, and compute honest per-stream coverage states.

**Requirements:** R3, R4, R9

**Dependencies:** U2, U3

**Files:**
- Create: `macos/Screencap/Views/Search/SearchViewModel.swift`
- Test: `macos/ScreencapTests/SearchViewModelTests.swift`

**Approach:**
- Fan out to the three verbs as **independent** calls (one stream's failure must not fail the others). Timeline gets the parsed window server-side with explicit `limit=200`; **content** hits are filtered client-side by their `timestamp_ms`; **transcript** hits have no `timestamp_ms` and must first be resolved `chunk_index`→chunk-start time, then filtered (unresolvable ones surface unanchored, off the timeline).
- Merge into one pointer list ranked by relevance+recency (blend deferred to implementation; tune against real data).
- Compute coverage state by reading `content.search`'s `index_state` and each verb's `coverage` field (`index_state` is content-only; transcript/timeline are `coverage`-only), defaulting pessimistically **without** demoting a stream that returned hits.
- `@MainActor`, on-demand (no timer), `guard` against overlapping in-flight searches (mirror `RecordingsIndex`).

**Patterns to follow:** `RecordingsIndex` re-entrancy guard + `do/catch DaemonClientError`; `mcp/server.py` pessimistic coverage defaulting; `ReviewWindowViewModel` `@StateObject` shape.

**Test scenarios:**
- Happy path: all three return hits → merged, ranked, time-filtered list with correct per-stream coverage labels.
- Edge: consent-needed is derived from the `content_index_enabled` flag being off (NOT from `index_state` alone) — both `not_indexed` (absent index DB) and `no_match` (flag off, stale index present) map to "on-screen text not searchable" when the flag is off (drives U7).
- Covers AE3 (revised): per-recording "still indexing" is NOT derivable (`index_state` is global) → surface a single global "on-screen text still indexing / not enabled" state, not a per-recording one (see Open Questions).
- Edge: transcript hit → `chunk_index` is resolved to a chunk-start time before window-filtering/placement; an unresolvable chunk yields an unanchored result.
- Edge: only `content.search` carries `index_state`; a transcript/timeline stream that returned hits is never demoted to "no data" by pessimistic defaulting.
- Edge: timeline authoritative-empty + content/transcript empty → "nothing recorded then," NOT a generic "no results" (R9).
- Edge: parsed time window present → content hits outside the window are filtered by their `timestamp_ms`; transcript hits by resolved chunk-start; timeline filtered server-side with `limit=200`.
- Error/integration: one stream errors (`store_unavailable`) while others succeed → partial results returned with a per-stream "couldn't search X" signal, not all-or-nothing.
- Edge: a stream hits the 200 cap → a "showing first N" signal is set (for timeline, "first N" is earliest-first, not most-recent — surface that honestly).

**Verification:** unit tests drive the view-model with a fake `SearchService` across the coverage matrix; assert ranking order, time-filter behavior, and per-stream labels.

---

### U5. Swift: Search view, sidebar wiring, and per-day timeline results

**Goal:** The Search section UI — input, per-day timeline result rendering, honest coverage/empty states, local-only framing.

**Requirements:** R1, R5, R6, R7, R9

**Dependencies:** U4

**Files:**
- Modify: `macos/Screencap/Views/MainWindow.swift` (add `.search` to `SidebarSection`, a `NavigationLink` row with `magnifyingglass`, and a `case .search` arm; confirm `.onChange(of: section)` handling)
- Create: `macos/Screencap/Views/Search/SearchView.swift` (search field + results)
- Create: `macos/Screencap/Views/Search/SearchTimelineView.swift` (day strip + scrubbable per-day marker timeline + result card)
- Modify: `macos/project.yml` only if needed, then `cd macos && xcodegen generate`
- Test: `macos/ScreencapTests/SearchViewModelTests.swift` (view-level logic stays in the testable view-model; views themselves are not unit-tested per repo norms — see learning on MenuBarExtra/openWindow non-testability)

**Approach:**
- Mirror `RecordingsListView` structure (loading/empty/error branches, tappable rows). Owns a `@StateObject SearchViewModel`.
- Per-day timeline: a day strip (days containing hits, sourced from results / `RecordingsIndex.countsByDay`), defaulting to the most-recent day with a hit; markers placed by `timestamp_ms` within the selected day; each result card shows app/window + timestamp + snippet with matched text highlighted.
- Distinct visual states for: idle/empty-query, no-matches, authoritative-"nothing recorded then," not-indexed (consent CTA → U7), partial-stream failure (per-stream chip), daemon-down (reuse transport-failure UI), "showing first N."
- Persistent "searches only what's on this Mac" affordance (R7); never imply masked/excluded content is searchable.

**Patterns to follow:** `RecordingsListView` (rows/states/navigation), `MainWindow.detail` loading/error templates, `FirstRunPrivacyBanner` for the inline consent banner slot.

**Test scenarios:**
- Test expectation: view-rendering logic is exercised through `SearchViewModel` tests (U4); SwiftUI view bodies and `openWindow` are not unit-testable here (per the MenuBarExtra/openWindow learning) — covered by manual QA checklist.
- Manual QA: each coverage/empty/daemon-down state renders the correct copy; day strip defaults to most-recent-hit day; local-only affordance always visible.

**Verification:** Search section appears and is reachable independent of recordings-list load state; states render per the coverage matrix; project builds after `xcodegen generate`.

---

### U6. Swift: Review-at-timestamp deep-link (seek-on-open)

**Goal:** Open the native Review window from a search result and seek to the exact moment, handling evicted recordings gracefully.

**Requirements:** R5

**Dependencies:** U5

**Files:**
- Modify: `macos/Screencap/State/ReviewWindowOpener.swift` (entry point that carries an optional seek target out-of-band)
- Modify: `macos/Screencap/Views/Review/ReviewWindow.swift` / `ReviewWindowViewModel.swift` (apply a pending seek after state `.ready`; guard null/0 `startedAt`)
- Modify: `macos/Screencap/ScreencapApp.swift` only if the opener-bridge registration needs the new seek channel
- Test: `macos/ScreencapTests/ReviewSeekTargetTests.swift`

**Approach:**
- Keep the Review scene keyed on recording-name (preserve singleton dedup). Deliver "seek to `timestamp_ms`" via a NotificationCenter post (mirroring `.reviewWindowUploadSucceeded`); the Review view applies it once `videoModel` exists.
- Convert absolute `timestamp_ms` → relative seconds via `(timestamp_ms/1000 - startedAt)`, clamped ≥0; if `startedAt` is null/0, fall back to seek=0 (don't NaN/crash) — per the nullable-timing learning. Verify `content.search`'s `timestamp_ms` is absolute unix-ms (not a recording-relative offset) before relying on this single conversion across streams.
- Seek target by stream: content/timeline hits use the hit's `timestamp_ms`; **transcript** hits use the resolved chunk-start time (U4); a transcript hit with no resolvable chunk time opens the recording with no seek.
- If the recording no longer exists (evicted between index and click), surface a "recording no longer available" terminal state and drop the stale marker from results.

**Patterns to follow:** `.reviewWindowUploadSucceeded` NotificationCenter usage; `RedactionTimeline` absolute→relative conversion; `RecordingsListView` `rowError` + `.alert`.

**Test scenarios:**
- Happy path: a seek target with a valid `startedAt` converts to the correct relative seconds (pure conversion function tested directly).
- Covers AE2. Integration (manual QA): selecting a result opens Review at the moment and shows the real frame — no generated summary.
- Edge: null/0 `startedAt` → seek falls back to 0, no crash.
- Error path: target recording missing → "no longer available" state; marker pruned.

**Verification:** the absolute→relative conversion is unit-tested incl. the null-origin guard; manual QA confirms open-and-seek and the missing-recording path.

---

### U7. Swift: OCR-indexing consent flow

**Goal:** A one-time consent prompt that enables on-screen-text indexing on consent, with honest tri-state persistence and local-only reassurance.

**Requirements:** R10, R7

**Dependencies:** U1, U4, U5

**Files:**
- Create: `macos/Screencap/Controllers/ContentIndexConsentController.swift` (tri-state: never-asked / consented / declined; read+write via the `settings` invoker)
- Create: `macos/Screencap/Views/Search/ContentIndexConsentBanner.swift` (inline banner/sheet with Enable / Not-now)
- Modify: `macos/Screencap/Views/Search/SearchView.swift` (surface the prompt when triggered)
- Modify: `macos/Screencap/Models/PrivacyStatus.swift` (extend `SettingsEnvelope.Inner` with a `content_index_enabled` field so the consent trigger can read the flag's current state — required by the trigger fix)
- Test: `macos/ScreencapTests/ContentIndexConsentControllerTests.swift`

**Approach:**
- Fire only when free-text is present AND the `content_index_enabled` flag is **off** (read via `settings --json`) AND consent state == never-asked. Do NOT gate on `index_state == not_indexed` alone — it only signals an absent index file, so a flag-off user with a stale index gets `no_match` and would never be prompted. Never re-modal once decided.
- Consent → write `content_index_enabled=true` (via U1's surface, `PrivacyController` invoker pattern: optimistic `@Published` then re-fetch). Persist consent state separately from the enable flag so "declined" ≠ "off."
- Copy reaffirms local-only / no network, and that it indexes **newly recorded** screens (forward-only; existing history not retroactively indexed) — set that expectation honestly.
- Decline → persist `declined`; subsequent searches run on timeline+transcript with a quiet, dismissible "enable on-screen-text search" affordance (no nagging).

**Patterns to follow:** `PrivacyController` (`JSONInvoker`, optimistic update + re-fetch, `pendingToggles` guard, `hasPrivacySection` never-written detection); `FirstRunPrivacyBanner` (non-blocking banner).

**Test scenarios:**
- Happy path: never-asked + free-text + `not_indexed` → prompt fires; consent writes the flag and transitions to `consented`.
- Covers AE4. Edge: decline → state `declined`, flag stays off, prompt never re-fires; quiet affordance remains available.
- Edge: prompt does NOT fire for pure time/app queries (OCR irrelevant) or empty queries, or when already consented/declined.
- Edge: write failure → consent state not falsely advanced (latch on success; retry path), mirroring `ensureFirstLaunchModeWritten`.
- Privacy: consent copy states local-only + forward-only (no implied egress, no implied backfill).

**Verification:** controller unit tests cover the tri-state transitions, trigger condition, and write-failure latch with a fake invoker; manual QA confirms copy + one-time behavior.

---

## System-Wide Impact

- **Interaction graph:** new Search section reads from the daemon verbs (read-only) and `RecordingsIndex` (for day coverage); deep-link posts into the Review window via NotificationCenter. No capture/recording path is touched.
- **Error propagation:** verb failures surface as typed `DaemonClientError`/`envelopeError`; each stream fails independently (partial results), daemon-down reuses existing transport-failure UI.
- **State lifecycle risks:** dangling pointers (recording evicted between index and click) → graceful terminal state + marker prune; consent tri-state must not be resurrected by a failed write.
- **API surface parity:** the one new backend surface (`settings` read/write for `content_index_enabled`) follows the existing `_BOOL_KEYS` contract; no new daemon verb.
- **Integration coverage:** open-and-seek and missing-recording paths need manual QA (SwiftUI view + `openWindow` not unit-testable); the coverage-state matrix is unit-tested in the view-model.
- **Unchanged invariants:** the three daemon verbs, the content index (local-only, ALLOW-frames-only), the capture/redaction pipeline, and the Review window's recording-name identity are all unchanged. Search is purely additive and read-only; it never widens what was captured or what leaves the device.

---

## Risks & Dependencies

| Risk | Mitigation |
|------|------------|
| Forward-only indexing makes on-screen-text history feel empty at launch | Honest consent copy ("newly recorded screens"); timeline (all history) + transcript still search retroactively; backfill is an explicit deferred follow-up |
| Transcript hits lack precise timestamps → imprecise markers | Anchor at chunk start, label approximate; don't claim moment-precision for transcript results |
| Coverage states collapse into a misleading "No results" | Data-driven coverage matrix is a first-class view-model concern with unit tests across all `index_state`×`coverage` combinations |
| Adding Swift files without regenerating the Xcode project → phantom build errors | `cd macos && xcodegen generate` after adding files (documented learning) |
| Daemon down when Search opens | Reuse existing transport-failure UI; no silent failure |
| Depends on in-flight v1.1 native Review playback as the deep-link target | U6 only adds a seek-on-open entry point; if playback isn't ready, the seek lands wherever the current Review window supports — coordinate sequencing with the v1.1 work |

---

## Sources & References

- **Origin document:** [docs/brainstorms/2026-06-24-ask-your-history-search-requirements.md](docs/brainstorms/2026-06-24-ask-your-history-search-requirements.md)
- Verb contract: `src/screencap/daemon/app.py`, `src/screencap/daemon/schema.py`, `src/screencap/content_index.py`
- Coverage-defaulting precedent: `src/screencap/mcp/server.py`
- OCR flag: `src/screencap/config.py`, `src/screencap/cli/__init__.py`, `src/screencap/chunk_processor.py`
- UI surfaces: `macos/Screencap/Views/MainWindow.swift`, `Views/RecordingsListView.swift`, `Controllers/DaemonClient.swift`, `State/ReviewWindowOpener.swift`, `Controllers/PrivacyController.swift`
