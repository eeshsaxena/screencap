---
title: "feat: Expand local query parser — time expressions, app/site coverage, index-sourced vocabulary"
type: feat
status: active
date: 2026-06-29
deepened: 2026-06-29
origin: https://linear.app/zk-email/issue/SCR-179/expand-local-query-parser-time-expressions-app-coverage
---

# feat: Expand local query parser — time expressions, app/site coverage, index-sourced vocabulary

## Summary

Broadens the local, rule-based `QueryParser` (Swift) so common phrasings a non-technical user types in Ask-Your-History Search resolve to the right time/app filters: more time expressions (this/last month, explicit dates, "N days ago", "last N weeks", standalone parts-of-day, explicit-date ranges) and broader app/site recognition. Per the two scope decisions taken at planning, the known-app/site vocabulary is sourced **primarily from the recordings index** (a new daemon enumeration verb) with the static table as a deterministic fallback, and `timeline.query` is extended to **filter** browser-based usage by the domain derived from `browser_url` — without ever returning the URL, so OAuth codes / session tokens captured pre-scrubber are never exposed. Still fully local, still no ML, still table-driven with deterministic `now` injection; unrecognized tokens keep falling through to free text.

---

## Problem Frame

Ask-Your-History Search (SCR-174, PR #283) shipped an intentionally thin `QueryParser`: ~20 hardcoded app names and a handful of time phrases. Anything outside that set silently falls through to plain keyword search, so the "ask your history" promise feels brittle the moment a user phrases a query naturally ("what was I doing last month", "the Stripe dashboard 3 days ago", "June 3"). SCR-174's plan explicitly deferred parser breadth to a follow-up (origin: `docs/plans/2026-06-24-002-feat-ask-your-history-search-plan.md`, U3 deferred note: *"which time expressions + app-name normalizations to support in v1"*). This is that follow-up. The on-device intent model remains a separate, later step — this work stays rule-based.

A second, structural gap surfaced during planning: the current `knownApps` list mixes desktop apps (Slack, Xcode, Zoom) with **web services** (Gmail, GitHub, Jira, Salesforce, Zendesk). But `timeline.query` only substring-matches `app_name` / `app_bundle_id`; for browser-based sites the app is "Safari"/"Chrome", so those tokens match **zero** timeline rows today — the site signal lives only in `browser_url`, which v1 deliberately omits. Routing site tokens to an `appFilter` that can never match is a latent UX trap this plan closes.

---

## Requirements

- R1. Broaden time expressions to resolve to absolute windows under a deterministic injected `now`: "this/last month", explicit dates ("June 3"), relative offsets ("3 days ago", "last N weeks", "last N days"), standalone parts-of-day ("afternoon" without this/last), and explicit-date ranges ("June 1 to June 3"). *(origin: SCR-179 "Do" bullet 1; SCR-174 R2)*
- R2. Broaden and normalize app/site recognition: more apps, and browser domains normalized to canonical tokens ("github.com" → "github", "mail.google.com" → "gmail"). *(origin: SCR-179 "Do" bullet 2)*
- R3. Source the known-app/site vocabulary **primarily from the recordings index** (distinct `app_name`/`app_bundle_id` + visited `browser_url` domains via a new daemon verb), with the static table as a deterministic fallback/seed. *(origin: SCR-179 "Do" bullet 2 — "consider sourcing known apps from the recordings index"; resolved to index-primary at planning)*
- R4. Make site/app filters actually match browser-based usage: `timeline.query` filters on the **domain** derived from `browser_url`, in addition to app name/bundle — **without** adding `browser_url` (or any full URL) to the response. *(planning decision on SCR-179 "browser domains → app names")*
- R5. Keep it table-driven and unit-tested (extend `QueryParserTests`), preserve deterministic `now` injection, and keep unrecognized tokens falling through to free text. *(origin: SCR-179 "Do" bullet 3 + Acceptance)*
- R6. The `apps.list` response and the in-app vocabulary derived from it are a **same-EUID-only, browsing-profile-class local artifact** (a deduped list of visited hostnames spells out the user's web-service / employer / healthcare relationships). It is treated with the same sensitivity class and "never crosses the EUID boundary" rule as `recording.db`: never logged in full, never written to any sync / export / telemetry / crash-report / cloud path. *(planning + security-deepening decision)*

**Origin actors:** A1 (non-technical end user searching their own history).
**Origin flows:** F1 (type natural-language query → structured time/app filters → ranked moments on a timeline).
**Origin acceptance examples:** common phrasings resolve to the right time/app filters; new cases covered by table tests; unrecognized tokens still fall through to free text. (From SCR-179 "Acceptance"; AE-linked in unit test scenarios below.)

---

## Scope Boundaries

- No ML / on-device intent model — that is the explicitly separate later step (origin: SCR-174 "Deferred for later"). This work stays rule-based.
- No new network surface; everything stays inside the same-EUID daemon trust boundary (`SECURITY.md`).
- No locale/i18n beyond English phrasings in v1.
- `browser_url` (and any full URL) is **never returned** to any caller or rendered in the UI — only the host/domain is used internally as a filter predicate. Returning/displaying `browser_url` stays out of scope (and remains a privacy non-goal).
- The `apps.list` hostname vocabulary (R6) is never synced, exported, uploaded, telemetered, or placed in a crash report / support bundle. It stays a same-EUID-only in-memory artifact.
- No change to `content.search` / `transcript.search` semantics; only `timeline.query` matching widens.

### Deferred to Follow-Up Work

- Surfacing the matched domain or URL in timeline results (a richer "in github.com" chip): a separate UX + privacy-review task. This plan only *filters* by domain.
- An on-device intent model replacing the rule-based parser (origin SCR-174 deferred-for-later).
- Vocabulary verb result caching/persistence beyond an in-memory app-session cache, if profiling shows the live scan is too costly at large libraries.

---

## Context & Research

### Relevant Code and Patterns

- `macos/ScreenCap/Controllers/QueryParser.swift` — the pure, synchronous, `now`-injected parser. Current shape: `parse(_ raw:, now:) -> ParsedQuery { timeWindow?, appFilter?, freeText }`; private static `knownApps`/`partsOfDay`/`weekdays` tables; `extractTimeWindow` / `extractApp` strip recognized phrases and return the remainder. This is the table-driven pattern to extend.
- `macos/ScreenCapTests/QueryParserTests.swift` — table-driven tests using a fixed UTC `Calendar` + injected `now` (2026-06-24 15:30 UTC), with **independently computed** expected windows (not re-running parser phrase logic). Mirror this for all new cases.
- `src/screencap/daemon/app.py` `_run_timeline_query` (lines ~850–911) — builds `LIKE %token%` clauses against `app_name` / `app_bundle_id`, schema-tolerant via `has_column`. The single seam to extend for `browser_url` domain matching. Uses `escape_like` from `screencap.content_index`.
- `src/screencap/engine/convert.py` (lines ~178–195) — existing precedent for deriving `app_name` from `app_bundle_id` (last component titlecased) and extracting `domain = urlparse(browser_url).hostname`. Reuse this hostname-extraction approach in the daemon match path and the vocabulary verb.
- `src/screencap/daemon/schema.py` — `TimelineQueryRequest` / `TimelineRow` / `TimelineQueryResponse` models; `TimelineRow` docstring documents *why* `browser_url` is omitted. New `apps.list` request/response models go here.
- `macos/ScreenCap/Controllers/DaemonClient.swift` (`timelineQuery`, `TimelineQueryRequest`), `macos/ScreenCap/Controllers/SearchService.swift` (`SearchService` protocol + `LiveSearchService`), `macos/ScreenCap/Models/SearchResult.swift` (`TimelineRow` decodable) — the Swift seam for adding the vocabulary fetch and threading it into the parser.
- `macos/ScreenCap/Views/Search/SearchViewModel.swift` (init ~line 110, `parser: QueryParser = QueryParser()`) — where the parser is constructed and where an index-sourced vocabulary would be injected.

### Institutional Learnings

- CI only runs `pytest -m privacy` (+ the lock-policy test). The daemon-side `browser_url` matching and the vocabulary verb's "never returns a URL" guarantee are privacy-bearing → mark those tests `@pytest.mark.privacy` and keep them Vision-free, or CI won't run them. (memory: `project_ci_only_runs_privacy_lane`)
- In this worktree, run Python tests with `PYTHONPATH=src` (editable install may point at another worktree). (memory: `project_editable_install_worktree_pythonpath`)
- macOS app build/test via XcodeGen; there is a known flaky daemon-reconnect test. (memory: `project_macos_build_test`)
- Test philosophy: fewer, better tests targeting the changed path — not exhaustive padding. (memory: `feedback_test_philosophy`)

### External References

- None. Pure local date math + table-driven recognition + a local SQL filter. No high-risk external domain, strong local patterns — external research skipped (announced at planning).

---

## Key Technical Decisions

- **Single filter token, daemon OR-matches across app name / bundle / domain.** `ParsedQuery` keeps one `appFilter` token (not a separate `siteFilter`); `timeline.query` matches it against `app_name` OR `app_bundle_id` OR the `browser_url` hostname. Minimal Swift surface churn, and the parser stays agnostic to whether a token is "really" an app or a site.
- **`browser_url` matching is filter-only on the hostname; the URL is never returned.** The daemon reads `browser_url` into process memory, extracts the hostname (`urlparse`), and uses it only in the row-keep predicate. The response `TimelineRow` shape is unchanged (app + title + time). This is the privacy crux: matching adds **zero** new exposure (a same-EUID process can already read the column from disk — `SECURITY.md` line 99), because nothing token-bearing leaves the daemon. Matching on hostname (not the full URL) also keeps the predicate away from the path/query-string where secrets live.
- **The vocabulary verb returns raw distinct values; Swift owns normalization + synonyms.** The daemon stays dumb (distinct `app_name`/`app_bundle_id` + distinct `browser_url` hostnames, capped). Swift applies lowercasing, domain→canonical-token mapping, and the curated synonym map (e.g. `mail.google.com`/`gmail.com` → `gmail`). Keeps the privacy-sensitive daemon path simple and pushes presentation logic to the client. **The cost:** raw hostnames cross the response boundary into Swift session memory. This is acceptable *only* under the R6 carve-out (same-EUID-only, never exported). A considered alternative — daemon normalizes to canonical tokens + strips subdomains to eTLD+1 so raw hostnames never leave the daemon — was deferred (it needs a public-suffix list the daemon doesn't carry, and the synonym map needs near-full hostnames to disambiguate e.g. `mail.google.com` from other `google.com` services). Revisit if R6's carve-out proves hard to hold.
- **Vocabulary verb returns hostnames only, never full URLs — and the list is a browsing-profile artifact, not "low sensitivity."** Same invariant as R4 applied to enumeration. Correcting the earlier framing: a deduped hostname list is *not* "far less sensitive" in absolute terms — it is a browsing profile (R6). It is safe here only because it stays inside the same-EUID boundary and is carved out of every export path. Hostnames are **not** stripped of secret-adjacent subdomains in v1 (`company.okta.com`, tenant subdomains) — see Deferred to Implementation for the eTLD+1-stripping option.
- **No `API_SCHEMA_VERSION` bump.** `API_SCHEMA_VERSION` is a single global stamped into every envelope; the Swift `decodeResponse` throws `schemaMismatch` on any mismatch for *every* verb, so bumping it is a hard cutover that breaks all search verbs against a skewed daemon — the opposite of the intended tolerance. Both changes here are purely additive (a new verb; an unchanged `TimelineRow` shape), so follow the established additive precedent (`permissions` block + SCR-148 fields added with *no* bump — `schema.py:120,146`). Give `apps.list` its own per-verb `_APPS_LIST_API_VERSION = 1` (mirroring `_TIMELINE_QUERY_API_VERSION`). Degradation path: an older daemon 404s `/v0/apps.list` → Swift `appsList` throws → U5 fail-soft to static-only parser.
- **`browser_url` is read in a narrow try/except and never bound to a traceback-visible local.** The daemon's per-recording block logs with `exc_info=True` (`app.py:921`) and the handler with `logger.exception` (`app.py:~322`); a raw `browser_url` bound as a frame local would land in `~/.screencap/run/*.log` on any exception, contradicting "used only for predicate evaluation." Extract the hostname inside a tight `try/except → None` scope so the raw URL never escapes it and never appears in a logged traceback (also satisfies R6's "never logged in full").
- **Index-sourced vocabulary is primary; static table is the deterministic fallback/seed.** Tests inject a fixed vocabulary set, so the parser stays pure and deterministic given its inputs. The only non-deterministic element (the live fetch) lives in `SearchViewModel`/`SearchService` and is tested via the existing service seam (mock).
- **Explicit dates resolve to the most-recent-past occurrence.** "June 3" with `now`=2026-06-24 → 2026-06-03; "December 5" → 2025-12-05. Avoids returning a future (empty) window. Pinned by table tests.
- **The conservative ambiguous-token exclusion guard applies to dynamic vocabulary too.** Even if the index yields an app literally named "Mail"/"Docs", the curated exclusion list still suppresses it from `appFilter` so "find the docs about X" doesn't misfire.

---

## Open Questions

### Resolved During Planning

- **Vocabulary source** → Index-sourced primary, static table fallback (user decision).
- **Site-token routing** → Expand `timeline.query` to match `browser_url`, implemented as privacy-preserving domain-only filtering with an unchanged response (user decision + planning refinement).
- **App vs site as one token or two** → One token; daemon does the OR-match (Key Technical Decisions).

### Deferred to Implementation

- **Exact boundary semantics for "last N weeks" / "last N days"** — rolling trailing window vs calendar-aligned. Default proposed below (trailing complete days incl. today); pin the final choice with the table test rather than prose.
- **Range-grammar breadth** — how many range phrasings to support in v1 ("June 1 to June 3", "June 1-5", "between X and Y"). Start with "X to Y" over two explicit dates; expand only if cheap.
- **Vocabulary refresh cadence / cache lifetime** in the app (fetch once per search session vs on a timer). Tune against the live scan cost.
- **Vocabulary verb scan cost** at large libraries — whether the recordings cap + `SELECT DISTINCT … LIMIT` is sufficient or needs a cheaper distinct-value source.
- **eTLD+1 subdomain stripping for the hostname vocabulary** — whether to strip `company.okta.com` → `okta.com` to reduce sensitivity (R6 / security finding) and improve synonym-map hit rates. Deferred because correct eTLD+1 needs a public-suffix list; v1 returns raw hostnames under the R6 carve-out. Decide during U1.
- **Post-scrub `browser_url` values** — for recordings that completed scrubbing, `scrubber.py` rewrites `window_event.browser_url` (`scrubber.py:~2680`), so U1/U2 may see a redacted/altered hostname rather than the original. The vocabulary verb and the domain filter operate on **whatever value is present** and do not attempt to recover the pre-scrub hostname; "github.com missing from my vocabulary" for an old scrubbed recording is expected, not a bug. (Reading the local-only `recording.db` stays in-bounds.)

---

## High-Level Technical Design

> *This illustrates the intended approach and is directional guidance for review, not implementation specification. The implementing agent should treat it as context, not code to reproduce.*

Parser pipeline (unchanged shape, broader tables) and the new vocabulary + match seam:

```
                         ┌─────────────────────────────────────────────┐
 user query ──▶ QueryParser.parse(raw, now)                             │
                         │  1. extractTimeWindow  (broadened: month,    │
                         │     explicit dates, N-days/weeks-ago, ranges)│
                         │  2. extractApp  (static table  ∪  injected   │
                         │     index vocabulary  →  normalized token)   │
                         │  3. remainder → freeText                     │
                         └───────────────┬─────────────────────────────┘
                                         ▼
                         ParsedQuery { timeWindow?, appFilter?, freeText }
                                         │  appFilter (one token)
                                         ▼
   SearchViewModel ──▶ timeline.query(app: token) ──▶ daemon _run_timeline_query
                                                          WHERE time ∈ window
                                                          AND ( app_name   LIKE %token%
                                                             OR app_bundle LIKE %token%
                                                             OR host(browser_url) contains token )
                                                          SELECT app, title, time   ◀── browser_url NOT selected into response

   Vocabulary (R3):
   SearchViewModel ──▶ apps.list ──▶ daemon: distinct app_name/bundle + distinct host(browser_url), capped
                          │             (hostnames only — never full URLs)
                          ▼
                  Swift normalizes (lowercase, domain→token, synonym map) ──▶ injected into QueryParser
                          │  (static table = fallback/seed; tests inject a fixed set for determinism)
```

The privacy invariant is the single load-bearing constraint: `browser_url` is read inside the daemon for *predicate evaluation and hostname enumeration only*; no full URL crosses the response boundary in either verb.

---

## Implementation Units

### U1. Daemon: app/site vocabulary enumeration verb (`/v0/apps.list`)

**Goal:** A read-only daemon verb that enumerates the distinct known apps and visited domains across local recordings, so the Swift parser can source its vocabulary from real usage instead of a static list (R3).

**Requirements:** R3

**Dependencies:** None

**Files:**
- Modify: `src/screencap/daemon/app.py` (new handler `apps_list` + `_run_apps_list` helper, register route `/v0/apps.list`, exclude from `_ACTIVITY_PATHS` like the other query verbs; **also exclude from audit logging**, consistent with the read-only query verbs — `recording.list` precedent in `SECURITY.md`)
- Modify: `src/screencap/daemon/schema.py` (`AppsListRequest` / `AppsListResponse` + an entry model; add a per-verb `_APPS_LIST_API_VERSION = 1` mirroring `_TIMELINE_QUERY_API_VERSION` — **do not** bump the global `API_SCHEMA_VERSION`; see Key Technical Decisions)
- Test: `tests/daemon/test_apps_list.py` (new)

**Approach:**
- Mirror `_run_timeline_query`'s structure: iterate `_iter_recording_dirs(None)` (respect the existing recordings cap), open each `recording.db` read-only, schema-tolerant via `has_table`/`has_column`.
- Collect `SELECT DISTINCT app_name`, `SELECT DISTINCT app_bundle_id` from `window_event`; and distinct **hostnames** from `browser_url` by reading the column and applying `urlparse(...).hostname` **inside a tight `try/except → None` scope** (reuse the `convert.py` precedent) so the raw URL never escapes to a frame local or a logged traceback (Key Technical Decisions log-hygiene invariant). Apply a per-query distinct cap.
- Response carries app display names + bundle ids + a deduped, lowercased **hostname** list — **never full URLs** (R4/R6 invariant). v1 does not strip subdomains (eTLD+1 stripping is a Deferred-to-Implementation option).
- Validate/bound inputs at the boundary like the other verbs; class-name-only diagnostics; pointer-/value-only (no paths). Truncation when the distinct cap is hit is signaled in a diagnostic/`log`, not silent.

**Patterns to follow:**
- `_run_timeline_query` / `timeline_query` handler pair in `src/screencap/daemon/app.py` (schema tolerance, recordings cap, route registration, `_ACTIVITY_PATHS` exclusion).
- `convert.py` hostname extraction.

**Test scenarios:**
- Happy path: two recordings with `window_event` rows across Slack (bundle `com.tinyspeck.slackmacgap`) and a browser visiting `https://github.com/...` → response contains `slack`-bearing app/bundle entries and host `github.com`; deduped across recordings.
- Edge case: recording whose `window_event` lacks `app_name` and/or `browser_url` columns (old schema) → tolerated, contributes only what it has, no crash.
- Edge case: empty recordings dir / no `recording.db` → empty vocabulary, `ok=true`.
- Edge case: distinct cap honored (synthesize > cap distinct apps → response length bounded, `log()`/diagnostic notes truncation rather than silently capping).
- Edge case: malformed/garbage `browser_url` value → the `try/except → None` path is taken, the row contributes no hostname, no exception escapes (guards the log-hygiene invariant).
- **Covers R3 / R6 / privacy.** `browser_url` carrying a token-bearing path (`https://example.com/cb?code=SECRET`) → response contains host `example.com` only. **Structural assertion (not just string-search):** every hostname entry contains no `/`, `?`, or `=` character (i.e. is a bare host as `urlparse(...).hostname` returns), and the raw URL / `SECRET` / path appears nowhere in the serialized response. Mark `@pytest.mark.privacy`.

**Verification:** `POST /v0/apps.list` returns distinct apps + hostnames across recordings, never a full URL, within the recordings cap; tolerant of old schemas.

---

### U2. Daemon: `timeline.query` filters by `browser_url` domain (privacy-preserving)

**Goal:** Make a single filter token also match browser-based usage by filtering on the hostname derived from `browser_url`, without changing the response shape or exposing the URL (R4). **This is the highest-risk unit and is bigger than a one-clause extension** — it restructures the query's limit/scan ordering (see Approach).

**Requirements:** R4

**Dependencies:** None (independent of U1; both touch `app.py` but different functions — sequence to avoid merge churn)

**Files:**
- Modify: `src/screencap/daemon/app.py` `_run_timeline_query` (restructure limit ordering + extend the keep-predicate; read `browser_url` internally only when the column exists and a filter token is present; never add it to the emitted row)
- Modify: `SECURITY.md` (narrow the "omits `browser_url`" statement: still never *returned*, but now read internally as a domain *filter* predicate — zero-new-exposure rationale; **and** document the `apps.list` hostname vocabulary as a same-EUID-only browsing-profile artifact derived from `recording.db`, echoing the narrowed-R7 content-index pattern — never crosses the EUID boundary)
- Modify: `src/screencap/daemon/schema.py` `TimelineRow` docstring (clarify: omitted from output; used as an internal domain filter)
- Test: `tests/daemon/test_timeline_query.py` (extend; create if absent)

**Approach:**
- **Limit-ordering restructure (load-bearing).** Today the SQL applies `... WHERE (app_name LIKE ? OR app_bundle_id LIKE ?) ... ORDER BY timestamp LIMIT ?` — the `LIMIT` runs *inside* SQL, *after* the app predicate. A browser row (`app_name='Safari'`, url=`github.com`) fails that `WHERE` and never reaches a post-SQL Python hostname filter, so "filter hostnames in Python after the SQL limit" (the naive approach) returns **zero** browser rows. Fix: when a filter token is present and `browser_url` exists, the SQL `WHERE` must **not** pre-filter those rows out, and the user `limit` must move to **after** the Python hostname filter. Concretely: select time-bounded candidate rows (including `browser_url` for internal use) with an **internal per-recording hard cap** (re-establishing the DoS bound the in-SQL `LIMIT` previously gave — do not select unbounded), extract the hostname per row in the tight `try/except → None` scope, apply the OR(`app_name`/`app_bundle_id`/`hostname`) predicate in Python, **then** apply the user `limit` / the cross-recording `heapq.nsmallest` merge. State the internal cap explicitly so the DoS guard isn't silently dropped.
- The desktop-app-only fast path (no `browser_url` column, or token clearly an app) may keep the existing in-SQL `LIKE` + `LIMIT` to avoid scanning; the restructure only applies when hostname matching is in play. Pick and document one path so behavior is predictable.
- Read `browser_url` into the daemon process **only** for predicate evaluation, inside the tight `try/except → None` scope (never bound to a traceback-visible local — log-hygiene invariant); do **not** include it in the response dict. The output row stays exactly `{recording, timestamp_ms, app, title}`.
- Match on the **hostname**, never the full URL, so the predicate never touches the path/query string where tokens live. Keep `has_column` tolerance: no `browser_url` column → behave exactly as today.

**Execution note:** Start with a failing privacy test asserting the returned row's key set is exactly `{recording, timestamp_ms, app, title}` even when matching on `browser_url`, then implement the predicate — the invariant is the load-bearing part.

**Patterns to follow:**
- Existing clause-building + `escape_like` usage in `_run_timeline_query`.
- `convert.py` hostname extraction.

**Test scenarios:**
- **Covers R4.** Browser recording visiting `https://github.com/issues` with token "github" → the row is returned (matched via domain) with `app` = the browser name and `title` present. **Structural assertion:** `set(row.keys()) == {"recording", "timestamp_ms", "app", "title"}` — not merely "no `browser_url` field" (regression-proof against a future `dict(row)` / `row_factory` refactor that re-adds the column).
- **Covers R4 / privacy.** `browser_url` = `https://app.example.com/oauth?code=SECRET&state=XYZ`, token "example" → row matched via hostname `app.example.com`; assert the exact key set above **and** that `SECRET`/`code=`/the full URL appear nowhere in the serialized response. Mark `@pytest.mark.privacy`.
- **Covers R4 (the ordering bug).** Construct rows so the matching browser row sorts *after* `limit` desktop rows in the time window → assert the browser row is still returned (proves the user `limit` is applied after the hostname filter, not before).
- Happy path (regression): desktop-app token "slack" still matches `app_name`/`app_bundle_id` exactly as before, response unchanged.
- Edge case: old recording with no `browser_url` column → unchanged behavior, no crash.
- Edge case: token matches neither app columns nor any hostname → row excluded (no false positives).
- Edge case: `browser_url` present but unparsable / null hostname → treated as non-match via the `try/except → None` path, no exception, no traceback emitted.

**Verification:** A site token returns the right browser-based timeline rows even when they sort past `limit`; the response key set is exactly `{recording, timestamp_ms, app, title}` (no URL); the internal scan cap bounds work per recording; old schemas unaffected; privacy tests green under `pytest -m privacy`.

---

### U3. Swift: broaden time expressions in `QueryParser`

**Goal:** Recognize the broader set of natural time phrasings and resolve each to an absolute `TimeWindow` under the injected `now` (R1).

**Requirements:** R1, R5

**Dependencies:** None (pure Swift; can land in parallel with U1/U2)

**Files:**
- Modify: `macos/ScreenCap/Controllers/QueryParser.swift` (extend `extractTimeWindow` + add month/explicit-date/relative/range helpers; keep most-specific-first ordering)
- Test: `macos/ScreenCapTests/QueryParserTests.swift` (extend, table-driven)

**Approach:**
- Add, ordered most-specific-first so longer phrases win before shorter ones:
  - **Months:** "this month" → `[startOfMonth(now), startOfNextMonth(now))`; "last month" → previous month.
  - **Explicit dates:** "june 3" / "jun 3" / "3 june" → that month/day resolved to the **most-recent-past** occurrence relative to `now` (Key Technical Decisions); full-day window.
  - **Relative offsets:** "3 days ago" → the full day N days before today; "last N weeks" / "last N days" → trailing window of N·7 / N complete days up to and including today (default: `[startOfDay(now) − N·7 days, startOfDay(now)+1 day)`; pin exact boundary by test).
  - **Standalone parts-of-day:** confirm "afternoon"/"morning"/"evening" without this/last already resolve to today's part (existing branch) and add coverage.
  - **Explicit-date range:** "june 1 to june 3" → `[startOfDay(june 1), startOfDay(june 3)+1 day)`; start with the "X to Y" form over two explicit dates.
- Keep the `strip(phrase)` + return-remainder pattern; everything not consumed flows to `freeText`.
- Reuse the existing `ms`, `fullDayWindow`, `weekWindow`, `dayPartWindow` helpers; add `monthWindow`, `explicitDateWindow`, `relativeDaysWindow`, `dateRangeWindow`.

**Patterns to follow:**
- Existing `extractTimeWindow` ordering + `Self.partsOfDay`/`weekdays` static tables; add a `months` static table.
- `QueryParserTests` independent-reference-window style (compute expected windows by hand, not via parser internals).

**Test scenarios:**
- **Covers AE.** Happy path: "this month" / "last month" → correct month windows for `now`=2026-06-24 (June / May).
- **Covers AE.** Happy path: "errors june 3" → 2026-06-03 full day; free text "errors"; no app.
- Happy path: "stripe dashboard 3 days ago" → 2026-06-21 full day (note: app recognition is U4; here assert the window + that "3 days ago" is consumed).
- Happy path: "last 2 weeks" → trailing-14-complete-days window per the pinned boundary.
- Happy path: standalone "afternoon" → today 12:00–18:00.
- Happy path: "june 1 to june 3" → 2026-06-01 00:00 .. 2026-06-04 00:00.
- Edge case: explicit date in the future relative to `now` ("december 5") → resolves to 2025-12-05 (previous year), not future.
- Edge case: "last 0 weeks" / "last 99 weeks" → no crash; sane window or graceful fall-through (define which; test it).
- Edge case: ambiguous/unparsable date ("june") → no window, "june" stays in free text.
- Regression: existing "today"/"yesterday"/"last tuesday"/"this morning"/"this week"/"last week" still pass unchanged.

**Verification:** Table tests assert exact absolute ranges for the fixed `now`; parser stays pure/deterministic; unrecognized date-ish tokens fall through to free text.

---

### U4. Swift: app/site recognition — static expansion, domain normalization, injectable vocabulary

**Goal:** Recognize a broader, normalized set of app/site tokens, sourcing the vocabulary primarily from an injected index-derived set with the static table as fallback/seed, while preserving the conservative ambiguity guard (R2, R3).

**Requirements:** R2, R3, R5

**Dependencies:** U1 (vocabulary data shape), U3 (shared `QueryParser` internals — sequence to avoid conflict)

**Files:**
- Modify: `macos/ScreenCap/Controllers/QueryParser.swift` (accept an optional injected vocabulary at construction; expand the static table; add domain→token normalization + a curated synonym map; apply the exclusion guard to all sources; support multi-word app names)
- Test: `macos/ScreenCapTests/QueryParserTests.swift` (extend)

**Approach:**
- Add an `init(calendar:, knownApps:)`-style seam: **change `QueryParser.knownApps` from a `private static let` to instance state seeded via the initializer** (the expanded static table becomes the default argument). The parser takes a vocabulary set (normalized tokens). Default = the expanded static table; live wiring (U5) passes the index-sourced set unioned with the static seed. Because the exclusion guard was implicitly a property of the curated static list, it must move to instance logic that runs over the injected set too (already required below).
- Normalization (Swift-owned, per Key Technical Decisions): lowercase; map domains to canonical tokens ("github.com" → "github", "mail.google.com"/"gmail.com" → "gmail", "linear.app" → "linear"); apply a small curated synonym map for well-known services.
- Preserve the conservative exclusion list (docs/mail/word/teams/sheets…) and apply it to dynamic vocabulary too, so an index app literally named "Mail" does not misfire `appFilter`.
- Support multi-word app names ("google chrome", "visual studio code") via phrase matching before single-token matching.
- Keep `extractApp`'s strip-and-return-remainder contract.

**Patterns to follow:**
- Existing `Self.knownApps` + `extractApp` loop and the documented exclusion rationale comment.
- `convert.py` domain extraction as the conceptual basis for the synonym map's domain side.

**Test scenarios:**
- **Covers R2.** Happy path: "github" and "github.com" both normalize to token "github"; "gmail" and "mail.google.com" both → "gmail".
- **Covers R3.** Injected-vocabulary: an app present only in the injected set (e.g. "superhuman") not in the static table → recognized as `appFilter`; same parser with only the static vocab → falls through to free text.
- Happy path: multi-word "google chrome errors today" → app "google chrome" (or its normalized token), free text "errors", today window.
- Edge case (ambiguity guard): "find the docs about pricing" → no `appFilter` even if "docs"/a Docs app is in the injected vocabulary; "docs" stays free text.
- Edge case: token appearing both as an app and inside free text only once → consumed once, remainder correct.
- Regression: existing `knownApps` (salesforce, slack, …) still recognized; `testUnrecognizedAppStaysInFreeText` still passes.

**Verification:** App/site tokens from both the static seed and the injected index vocabulary resolve to a single normalized `appFilter` token; ambiguous words still fall through; parser remains pure given its injected vocabulary.

---

### U5. Swift: wire index-sourced vocabulary into Search

**Goal:** Fetch the index-derived vocabulary from the new daemon verb and construct the live `QueryParser` with it, falling back to the static table on any failure (R3).

**Requirements:** R3, R5

**Dependencies:** U1 (verb), U4 (parser accepts injected vocabulary)

**Files:**
- Modify: `macos/ScreenCap/Controllers/DaemonClient.swift` (`appsList` method + `AppsListRequest`/`AppsListResponse` request types)
- Modify: `macos/ScreenCap/Controllers/SearchService.swift` (`SearchService` protocol + `LiveSearchService` forwarding)
- Modify: `macos/ScreenCap/Models/SearchResult.swift` (decodable `AppsListResponse` / entry model)
- Modify: `macos/ScreenCap/Views/Search/SearchViewModel.swift` (fetch vocabulary, normalize via U4 helpers, build/refresh the parser; fall back to a static-only parser on throw; cache for the session)
- Test: `macos/ScreenCapTests/SearchViewModelTests.swift` (extend or create — drive via the mock `SearchService` seam)

**Approach:**
- Add `appsList` to the `SearchService` seam mirroring `timelineQuery` so the view model is testable without a live daemon.
- **Change `SearchViewModel.parser` from `let` to `var`** (`QueryParser` is a value `struct`, so reassignment is clean). On search-session start (cadence is a deferred tuning question), fetch the vocabulary, normalize it Swift-side (via U4 helpers), union with the static seed, and **rebuild** the `QueryParser` with the combined set. **Define the refresh point relative to `search()` reading `self.parser`** (`SearchViewModel.swift:134`): rebuild on the first `search()` of a session *before* parsing, guarded (e.g. an in-flight flag) so a concurrent refresh on `@MainActor` doesn't race the parse. Note: the `init` default arg constructs `QueryParser()` (static-only) — the live path must *explicitly* overwrite it with the index-unioned parser, or it silently stays static-only.
- Fail-soft: an older daemon 404s `/v0/apps.list` (route absent) or any throw → keep the static-only parser; search stays fully usable (mirror the existing `persistBackfillDeclined` "throwing surfaces as a no-op" posture).
- Determinism preserved: tests inject a fixed vocabulary via the mock service; the parser itself stays pure.

**Patterns to follow:**
- `SearchService`/`LiveSearchService` protocol-seam + `DaemonClient.timelineQuery` request/response style.
- `SearchViewModel`'s existing dependency-injection init and fail-soft `@Sendable` closures.

**Test scenarios:**
- Happy path: mock `appsList` returns `["superhuman", "github.com"]` → a subsequent `search("superhuman notes today")` yields `appFilter` = "superhuman"; "github.com" usable as a site token. **Assert the live path actually injected the index vocabulary** (recognizes a token *only* present in the mock set, not in the static table) — not merely that fallback works.
- Error path: mock `appsList` throws (and the 404/older-daemon case) → parser falls back to static vocabulary; search still returns results; no crash, no surfaced error blocking search.
- Edge case: empty vocabulary response → behaves exactly as static-only.
- Integration: vocabulary fetched once per session is reused across multiple `search(...)` calls (assert the mock is not re-hit per keystroke, per the chosen cadence); the refresh does not race the first parse.

**Verification:** Live search recognizes apps/sites that exist in the user's recordings but not the static list; a daemon/vocabulary failure degrades gracefully to static recognition.

---

## System-Wide Impact

- **Interaction graph:** `timeline.query`'s `app` filter semantics widen (now also matches `browser_url` domains). The MCP server (`screencap mcp`) forwards `timeline.query` unchanged — same response shape, broader matches. Document in the verb's docstring.
- **API surface parity:** New `/v0/apps.list` verb adds to the daemon API. **Do not bump the global `API_SCHEMA_VERSION`** — both changes are additive (new verb; unchanged `TimelineRow`), and a global bump is a hard cutover that makes `decodeResponse` throw `schemaMismatch` on *every* verb against a skewed daemon (follow the `permissions`/SCR-148 additive precedent — `schema.py:120,146`). Tolerance is structural, not version-negotiated: an older daemon simply 404s the new route → Swift fail-soft to static-only vocabulary (U5).
- **Error propagation:** Vocabulary fetch is fail-soft end-to-end (daemon error → Swift throw → static fallback). `timeline.query` domain matching must not raise on unparsable URLs (treat as non-match).
- **State lifecycle risks:** None persistent — vocabulary is an in-memory session cache; no new on-disk state.
- **Integration coverage:** The "site token → domain-matched timeline row with no URL in the response" path crosses Swift→daemon→SQLite and is only proven by the daemon privacy test (U2) plus the Swift integration test (U5); unit mocks alone won't catch a regression that re-adds `browser_url` to the projection.
- **Unchanged invariants:** `recording.db` stays local-only and is never uploaded; `content.search`/`transcript.search` semantics are untouched; `browser_url` (and any full URL) is still never returned to any caller or rendered — this plan only adds it as an internal filter/enumeration predicate.

---

## Risks & Dependencies

| Risk | Mitigation |
|------|------------|
| **`browser_url` token leakage via response** — a refactor accidentally adds `browser_url` to the `timeline.query` projection or the `apps.list` response, exposing OAuth/session tokens captured pre-scrubber. | Hard invariant: read `browser_url` only for predicate/hostname; never `SELECT` it into the response dict. `@pytest.mark.privacy` tests in U1/U2 assert the **exact key set** (structural, not string-search) and run on CI's privacy lane. |
| **`browser_url` token leakage via logs** — an exception in the hostname-extraction block emits a traceback (`exc_info=True`) binding the raw URL as a frame local into `~/.screencap/run/*.log` (not under the `recording.db` never-upload rule; may reach support bundles). | Extract hostname in a tight `try/except → None` scope; never bind the raw URL to a traceback-visible local. Test the malformed-URL path. |
| **The `apps.list` hostname list is a browsing profile** that a future feature (cloud sync, telemetry, crash report) could exfiltrate, turning a local artifact into an externally-visible profile. | R6 carve-out: documented same-EUID-only sensitivity class (parallel to `recording.db`), explicitly excluded from every export path, recorded in SECURITY.md. eTLD+1 subdomain stripping deferred as a further mitigation. |
| Matching against the full URL would touch the secret-bearing path/query string. | Match on `urlparse(...).hostname` only; never the path/query. |
| **U2 limit-ordering bug** — naive "filter hostnames in Python after the SQL `LIMIT`" returns zero browser rows (the in-SQL app predicate + `LIMIT` drop them first). | Restructure: time-bounded select with an internal scan cap, hostname filter in Python, user `limit`/`nsmallest` applied *after*. Test that a browser row sorting past `limit` is still returned. U2 flagged as the highest-risk unit. |
| Vocabulary scan cost grows with library size. | Respect the existing recordings cap; `SELECT DISTINCT … LIMIT`; cache Swift-side per session; truncation is logged, not silent. Cheaper distinct-value source deferred. |
| Dynamic vocabulary reintroduces ambiguous tokens (an app named "Mail"/"Docs"). | The conservative exclusion guard applies to all vocabulary sources, not just the static table. |
| Daemon/Swift skew breaks search. | Additive only — **no global `API_SCHEMA_VERSION` bump**; `has_column` tolerance daemon-side; older daemon 404s the new route → fail-soft to static vocabulary Swift-side. |
| "last N weeks" boundary semantics are a judgment call and easy to get subtly wrong. | Pin the exact boundary with an explicit table test rather than prose; treat the test as the spec. |

---

## Documentation / Operational Notes

- Update `SECURITY.md` (U2) to reflect the narrowed posture: (a) `browser_url` is still never *returned*, but is now read internally as a domain filter/enumeration predicate, with the zero-new-exposure rationale and the log-hygiene constraint stated; (b) the `apps.list` hostname vocabulary is a same-EUID-only browsing-profile artifact derived from `recording.db` (echo the narrowed-R7 content-index pattern: same sensitivity class, never crosses the EUID boundary, never exported).
- Update the `TimelineRow` / `timeline.query` docstrings (`src/screencap/daemon/schema.py`, `app.py`) and add docs for `/v0/apps.list`.
- Consider a note in `docs/mcp-client-setup.md` if the widened `timeline.query` matching is worth signaling to MCP operators (optional).

---

## Sources & References

- **Origin issue:** [SCR-179 — Expand local query parser](https://linear.app/zk-email/issue/SCR-179/expand-local-query-parser-time-expressions-app-coverage)
- **Parent feature:** [SCR-174 — Ask-Your-History Search](https://linear.app/zk-email/issue/SCR-174/ask-your-history-search-in-app-v1) (PR #283); plan `docs/plans/2026-06-24-002-feat-ask-your-history-search-plan.md` (U3 deferred parser-breadth note), requirements `docs/brainstorms/2026-06-24-ask-your-history-search-requirements.md` (R2).
- Parser: `macos/ScreenCap/Controllers/QueryParser.swift`, `macos/ScreenCapTests/QueryParserTests.swift`
- Daemon timeline: `src/screencap/daemon/app.py` (`_run_timeline_query`), `src/screencap/daemon/schema.py` (`TimelineRow`)
- Domain extraction precedent: `src/screencap/engine/convert.py`
- Privacy boundary: `SECURITY.md` (browser_url omission rationale; same-EUID trust boundary)
