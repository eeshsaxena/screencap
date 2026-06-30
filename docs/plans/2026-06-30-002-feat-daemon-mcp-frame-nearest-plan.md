---
title: "feat: Add daemon /v0/frame.nearest + MCP tool (nearest-frame resolution primitive)"
type: feat
status: active
date: 2026-06-30
deepened: 2026-06-30
origin: https://linear.app/zk-email/issue/SCR-186/p2-add-daemon-v0-mcp-frameresolve-nearest-frame-primitive-scr-177
---

# feat: Add daemon /v0/frame.nearest + MCP tool (nearest-frame resolution primitive)

## Summary

Add a read-only daemon verb `POST /v0/frame.nearest` plus a matching MCP tool that maps a `(recording, timestamp_ms, staleness_cap_ms)` search pointer to the nearest on-disk screenshot **stem** (e.g. `"1719400010.000000"`) and a signed `delta_ms`, or a null miss. The verb is a faithful Python port of the unit-tested Swift `FrameSelection.nearest` + `loadFrames` + cap algorithm that SCR-177 added app-side, **plus a blocked-interval filter** so it never resolves to a frame the privacy pipeline masked or excluded (preserving the content index's ALLOW-only invariant end-to-end). No image bytes cross the wire — the agent has same-EUID filesystem access and constructs `~/.screencap/recordings/<name>/screenshots/<stem>.jpg` itself, preserving priv-R8. For transcript hits (which are chunk-granular: chunks default to 15 minutes and carry no per-word timing), `transcript.search` hits are additively enriched with a `timestamp_ms` (the chunk's `chunk_start` from its manifest), a `timestamp_granularity: "chunk"` marker, and `chunk_duration_ms`, so the agent can resolve a representative chunk frame with a chunk-scaled cap and label it honestly as coarse.

---

## Problem Frame

SCR-177 (PR [#288](https://github.com/proteus-computer-use/screencap/pull/288)) added nearest-frame resolution **app-side only**: the Swift `RecordingFrameIndex` / `FrameSelection` map a `(recording, anchorMs)` search pointer to the closest local `screenshots/*.jpg` within a ~30s staleness cap. The daemon query verbs (`/v0/content.search`, `/v0/transcript.search`, `/v0/timeline.query`) and the MCP server stay pointer-only (priv-R8): an agent receives `(recording, timestamp_ms)` — or `chunk_index` for transcript hits — but has **no supported way** to answer "which frame was on screen at that moment" without re-implementing the `{epoch:.6f}.jpg` listing + millisecond conversion + staleness logic. Withholding the thumbnail **bytes** is correct for priv-R8/priv-R4; the gap is the **resolution primitive**, not the image. Surfaced in the SCR-177 agent-native review.

---

## Requirements

> **Requirement-ID legend.** `R1`–`R9` below are **this plan's local** requirements. Citations written `priv-R4` / `priv-R8` / `priv-R9` refer to **project-wide privacy requirements** defined in `SECURITY.md` / `CLAUDE.md` — `priv-R4` (privacy / scrubbed-cloud-only), `priv-R8` (pointer-only query surface, no media bytes), `priv-R9` (recording-name-free diagnostics / no stale daemon-launch state). They are inherited constraints, not defined here; the `priv-` prefix keeps them from colliding with this plan's own `R4`/`R8`/`R9`.

- R1. Provide a read-only daemon verb mapping `(recording, timestamp_ms, staleness_cap_ms?)` → `{stem, delta_ms}` or a null miss. Response is pointer-only: a bare stem, never a path or image bytes. (origin: SCR-186 "Proposed"; preserves priv-R8)
- R2. The nearest-frame selection must match the Swift `FrameSelection` semantics: absolute-nearest by `abs(frame_ms − anchor_ms)`, inclusive `<=` staleness cap (default 30 000 ms), earliest-wins on an exact tie, round-half-**away-from-zero** at the ms boundary (Swift `.rounded()` default), `*.jpg`-only listing.
- R3. Provide a matching MCP tool that forwards to the verb over the existing async UDS client and returns the stem + delta (or a miss).
- R4. Make transcript hits resolvable: enrich `transcript.search` hits with `timestamp_ms` (chunk `chunk_start`), `timestamp_granularity: "chunk"`, and `chunk_duration_ms`, all derived from the per-chunk manifest (nullable when the manifest is absent).
- R5. Validate inputs at the boundary with typed 4xx errors: bad recording name; `timestamp_ms` outside `[0, max-epoch-ms]`; `staleness_cap_ms` outside `[0, upper-bound]`. A legitimate **miss** (over-cap / no eligible frames / missing screenshots dir) returns `ok:true` with null fields — never an error.
- R6. Preserve privacy invariants: no image bytes on the wire, class-name-only diagnostics, the verb stays out of `_ACTIVITY_PATHS`, and the resolution reads only the flat `screenshots/` listing plus `recording.db` blocked-interval geometry (no OCR text, no URLs, no rich per-word transcript JSON).
- R7. Re-read on-disk frames and blocked intervals per request — no caching of a recording's frame set or blocked geometry at daemon-launch time (the daemon is long-lived; recordings grow after launch). (priv-R9 staleness)
- R8. **`frame.nearest` must exclude frames inside `SCRUB_BLOCK_ACTIONS` blocked intervals** so it never points an agent at a masked / excluded / secure-field frame — preserving the content index's documented ALLOW-only invariant (`SECURITY.md` SCR-118) at the resolution layer. The blocked-interval read is **fail-closed**: if blocked geometry cannot be determined (recording.db missing/unreadable, deleted-row coverage gap, classification ambiguity), treat the range as blocked and return a miss rather than risk surfacing a sensitive frame.
- R9. Transcript-derived resolution is **chunk-granular** and must be labeled as such: the anchor is `chunk_start`, it is resolved with a chunk-scaled cap (not the 30s default), and its `delta_ms` is offset-from-chunk-start — explicitly **not** offset-from-matched-word (no per-word timing exists without crossing the priv-R7/R4 rich-transcript line). The contract must not let an agent mistake the chunk frame for the exact frame of a matched word.

**Origin actors:** local agent (MCP client) consuming search pointers; the daemon (same-EUID HTTP server over the UNIX socket).
**Origin flows:** search hit (content / timeline / transcript) → resolve nearest ALLOW frame → agent constructs the on-disk `.jpg` path itself.

---

## Scope Boundaries

- **No image bytes on the wire.** The verb returns a stem only; serving/encoding thumbnail bytes through the daemon or MCP is out of scope (the priv-R8/priv-R4 line this ticket deliberately does not cross).
- **No exposure of per-word transcript timing.** The rich per-word `transcript_*.json` (a priv-R7/R4 leak the codebase deliberately never reads) stays unread; transcript resolution is therefore chunk-granular by construction, not a temporary limitation.
- **No recording.db schema change.** Chunk timing comes from the existing on-disk `chunk_{idx:04d}_manifest.json`; blocked intervals come from existing `recording.db` geometry read by existing skip-interval logic. No new tables or columns.
- **No change to the Swift app-side resolver.** `RecordingFrameIndex` stays as-is; this ports its algorithm to Python and adds the blocked-interval filter the Swift side does not (the Swift app already constrains display to ALLOW frames upstream).
- **Not a generic time-range frame query.** Single nearest-anchor lookup only; range/window frame enumeration is out of scope.

### Deferred to Follow-Up Work

- Capturing the MCP-server / async-UDS-forwarding gotchas and the epoch-stem→ms parsing + blocked-interval-skip contract as a `docs/solutions/` entry (via `/ce-compound`) — currently undocumented gaps the next query-surface feature would re-discover.

---

## Context & Research

### Relevant Code and Patterns

- **Daemon query verbs** — `src/screencap/daemon/app.py`: `content_search`, `transcript_search` (`app.py:1121`), `timeline_query` (`app.py:1153`). All `async def name(request) -> JSONResponse`, registered as explicit `Route(..., methods=["POST"])` entries in `build_app()` (`app.py:1320`-`1344`). Canonical body: `await request.json()` → coerce non-dict to `{}` → `schema.*Request.model_validate(body)` → `validate_recording_name(...)` → `await asyncio.to_thread(_run_*, ...)` → `JSONResponse(schema.envelope(...))`, wrapped in `except DaemonAPIError → _api_error_response` / `except Exception → _internal_error_response`.
- **Blocked-interval / skip-interval derivation** — `src/screencap/backfill/skip_intervals.py` re-derives a recording's blocked intervals over the **intact** local `recording.db`, **fail-closed** on deleted-row coverage gaps + null-column classification ambiguity, honoring `PrivacyMode` from `.recording_intent`. The `backfill` package **does not import `screencap.daemon`** (destination-agnostic), so the daemon can import it without a cycle. `src/screencap/index_core.py` (`index_range`) consumes the live `ScrubResult.blocked_intervals` over the same `SCRUB_BLOCK_ACTIONS` set — the rule the resolution filter must match.
- **Idle-shutdown activity set** — `src/screencap/daemon/_idle_shutdown.py:52`: `_ACTIVITY_PATHS` holds only `/v0/recording.start` / `.stop`. Read verbs are never added; liveness is held by the MCP `/v0/events` subscription. Nothing to do to exclude `frame.nearest`.
- **Schema versioning** — `src/screencap/daemon/schema.py`: per-verb `_*_API_VERSION` constants (`:8`-`27`), lazy Pydantic models in `_MODEL_NAMES` (`:49`) + `_MODELS` (`:420`), `schema.envelope(schema_version=..., **payload)` (`:38`). Module stays Pydantic-free at import top-level.
- **Recording-dir resolution + name validation** — `src/screencap/config.py:526` `resolve_recording_dir` (`.resolve()` + `is_relative_to`) and `src/screencap/daemon/_name_validation.py` `validate_recording_name` (typed `invalid_name`). Python analogue of the Swift `screenshotsDir` containment guard.
- **Epoch-stem parser (reuse)** — `src/screencap/redaction/geometry.py:38` `parse_screenshot_timestamp(filename) -> float | None`. **Divergence note:** it accepts `.jpeg` as well as `.jpg`; the Swift lists `*.jpg` only. Reuse the parser but constrain the glob to `*.jpg`.
- **Glob/parse/sort idiom (reuse)** — `src/screencap/index_core.py:187`-`195`: `for img in screenshots_dir.glob("*.jpg"): ts = parse_screenshot_timestamp(...)` then `parsed.sort(...)` and `bisect`. No existing single-frame "nearest" helper — that selection is new.
- **MCP server** — `src/screencap/mcp/server.py`: module-level `async def` tools (`search_transcript` `:180`-`225`), registered by iterating a tuple through `mcp.tool()` in `build_server()` (`:278`-`303`); typed result models `:47`-`111`. Client `src/screencap/mcp/_client.py`: `AsyncDaemonClient` over `httpx` `AsyncHTTPTransport(uds=...)`; per-verb methods like `transcript_search` (`:112`-`120`) calling `self._post(...)`; `_post`/`_ok` validate the envelope.
- **chunk_index → timestamp source** — `src/screencap/task_manifest.py` `_generate_manifest_v2` (`:61`-`104`) writes `chunk_{idx:04d}_manifest.json` with `{"chunk_index", "chunk_start", "chunk_end", ...}` in **epoch seconds**. `chunk_duration_ms = round((chunk_end − chunk_start) * 1000)`. Transcript hits today derive `chunk_index` only, via `_parse_chunk_index` (`app.py:806`). `config.get_chunk_duration()` defaults to **900.0 s (15 min)** — the fact that forces R9's chunk-granular contract.

### Institutional Learnings

- **`docs/solutions/integration-issues/review-data-nullable-timing-swift-consumer-2026-06-01.md`** — Pin nullable-field contracts in one authoritative place. Drives the miss shape (`stem`/`delta_ms` null vs error) and the additive nullable transcript fields.
- **`docs/solutions/integration-issues/daemon-start-failure-typed-error-before-spawn-2026-06-08.md`** — Raise typed 4xx at the validation boundary, not generic 500s; don't cache in-process state at daemon launch (R7); port from *verified Swift behavior*, not prose.
- **`docs/solutions/runtime-errors/eventbus-late-listener-replay-2026-05-12.md`** — Daemon tests can pass while production stalls when stimulus arrives after the subscriber. Low-acute here (read-only request/response); still exercise the MCP→client→daemon path with real round-trips.
- **No `docs/solutions/` entry** exists for the MCP server, content-index epoch-stem parsing, or the priv-R8 convention — `SECURITY.md` + `CLAUDE.md` + the live `daemon/app.py` query verbs are the authoritative pattern source.

### External References

- None. Internal port + filter with strong local patterns (three existing `/v0/*.search` verbs, five existing MCP tools, existing skip-interval derivation); external research adds no value.

---

## Key Technical Decisions

- **POST verb, JSON body — not GET.** The ticket sketches `GET ...?`, but every existing `/v0/*.search` verb carrying required params is POST with a Pydantic-validated JSON body. Mirror that. (Verb path keeps the ticket's `frame.nearest`; the title's `frame.resolve` is an alias.)
- **Blocked-interval filtering is in-scope and fail-closed.** `frame.nearest` excludes any stem whose ms falls in a `SCRUB_BLOCK_ACTIONS` interval, so it never points an agent at a masked frame — preserving the content index's ALLOW-only invariant at the resolution layer. Reuse `backfill/skip_intervals.py`'s recording.db-backed derivation (already fail-closed). When blocked geometry is indeterminate, return a **miss** (privacy-safe), even though the rest of the system is fail-*open* — a resolution that could surface a sensitive frame is the one place fail-closed is correct. *(Rationale: the same-EUID agent can read any frame directly, so this is least-surprise / defense-in-depth, not a hard security boundary — but the documented ALLOW-only guarantee is exactly what an agent reasons from when it decides a daemon-returned stem is safe to surface.)*
- **Round-half-away-from-zero, to match Swift.** Swift's bare `.rounded()` is `.toNearestOrAwayFromZero`; Python's built-in `round()` is banker's (round-half-to-even) and **diverges at exact half-ms boundaries**. Use `int(math.floor(ts * 1000 + 0.5))` (valid for the non-negative epoch domain) for both frame stems and `chunk_start`. A parity fixture must land `ts*1000` exactly on `.5` ms so the rule is actually exercised (the existing Swift fixtures end in `.0/.25/.5` s → whole ms and never hit it).
- **Transcript resolution is chunk-granular, not 30s-cap-gated.** Chunks default to 15 min and carry no per-word timing, so a 30s cap against `chunk_start` would return null for ~99.9% of a chunk's words. Instead the transcript hit advertises `timestamp_ms` (chunk_start), `timestamp_granularity: "chunk"`, and `chunk_duration_ms`; the agent resolves with `staleness_cap_ms = chunk_duration_ms` to always get a representative chunk frame (subject to the blocked filter), labeled coarse. No new `frame.nearest` parameter — the cap mechanism already expresses this.
- **`delta_ms` is signed** = `chosen_ms − timestamp_ms`; the cap compares `abs(delta_ms) <= staleness_cap_ms`. For content/timeline anchors it is the true frame-to-moment offset. For transcript anchors it is offset-from-chunk-start (documented in R9), **not** distance to the matched word — the contract and docstring must say so to avoid misleading the agent.
- **Input upper bounds.** Bound `staleness_cap_ms` (e.g. 86 400 000 ms / 24h — comfortably above any chunk duration) and `timestamp_ms` (reject beyond a realistic max epoch-ms) at the Pydantic layer, mirroring the per-query caps existing verbs enforce (`SECURITY.md` SCR-118). `load_frames` is naturally bounded by a recording's screenshot count; note that bound rather than truncating the list (truncation could drop the true nearest).
- **Pure core stays pure; recording.db read lives in the adapter.** `src/screencap/frame_resolve.py` is filesystem-only and takes blocked intervals as a passed-in argument (no `screencap.daemon` import, mirroring `index_core.py`). The daemon adapter loads blocked intervals (U6) and frames (U1) and passes both into the pure selector — keeping U1 trivially unit-testable.
- **Glob `*.jpg` only** (match Swift) even though the reused `parse_screenshot_timestamp` also accepts `.jpeg`.
- **Re-read frames + blocked geometry per request** (no daemon-launch cache) — R7.

---

## Open Questions

### Resolved During Planning

- **Masked/blocked frames?** → `frame.nearest` filters `SCRUB_BLOCK_ACTIONS` blocked intervals (fail-closed) so it never resolves to a masked frame; preserves the ALLOW-only invariant. (user decision)
- **Transcript anchor vs 15-min chunks + 30s cap?** → Chunk-granular: anchor on `chunk_start`, resolve with a `chunk_duration_ms` cap, label `timestamp_granularity: "chunk"`; do not gate on the 30s default. (user decision)
- **Swift `.rounded()` vs Python `round()`?** → Swift is round-half-away-from-zero; use `floor(ts*1000 + 0.5)`, not Python `round()`. (feasibility review)
- **Input upper bounds?** → Add `staleness_cap_ms` and `timestamp_ms` upper-bound validation. (security review)
- **How do transcript hits become resolvable?** → Enrich `transcript.search` (chosen over a polymorphic `frame.nearest`, a separate resolve verb, or deferral). (user decision)
- **GET vs POST?** → POST, matching existing verbs.

### Deferred to Implementation

- Whether `backfill/skip_intervals.py` exposes a directly-callable "blocked intervals for recording over [t0,t1]" function or needs a thin shared extraction — settle when wiring U6; do not duplicate its fail-closed logic.
- Exact MCP tool function name (`resolve_frame` vs `nearest_frame`) and result-model field names — cosmetic, match `server.py` naming at write time.
- Whether the manifest reader is inline in the transcript helper or a small `_chunk_timing(recording_dir, chunk_index)` helper — favor a named helper for testability.
- Whether `_TRANSCRIPT_SEARCH_API_VERSION` should bump for the additive fields, and which existing schema-version test assertions need updating — confirm against `tests/daemon/test_read_only_verbs.py` so the bump doesn't silently break consumers.

---

## High-Level Technical Design

> *This illustrates the intended approach and is directional guidance for review, not implementation specification. The implementing agent should treat it as context, not code to reproduce.*

Agent resolution flow (transcript path now closes, with the blocked filter in the loop):

```mermaid
sequenceDiagram
    participant A as Agent (MCP client)
    participant D as Daemon (/v0)
    participant DB as recording.db (blocked geometry)
    participant FS as ~/.screencap/recordings/<name>

    A->>D: transcript.search(query)
    D-->>A: hits[{recording, chunk_index, snippet, timestamp_ms, timestamp_granularity:"chunk", chunk_duration_ms}]
    A->>D: frame.nearest(recording, timestamp_ms, staleness_cap_ms=chunk_duration_ms)
    D->>FS: list screenshots/*.jpg, parse stems → ms
    D->>DB: blocked intervals (SCRUB_BLOCK_ACTIONS, fail-closed)
    D-->>A: {stem, delta_ms}  (nearest ALLOW frame)  or  {stem:null, delta_ms:null}
    A->>FS: open screenshots/<stem>.jpg  (same-EUID; no bytes via daemon)
```

Pure core selection (port of Swift `FrameSelection` + cap, plus the blocked filter), directional:

```
nearest_frame(frames, anchor_ms, cap_ms = 30_000, blocked_intervals = ()):
    # frames: ascending [(ms, stem)] from screenshots/*.jpg
    eligible = [f for f in frames if not in_any(f.ms, blocked_intervals)]   # ALLOW-only
    if not eligible: return None
    chosen = min(eligible, key=lambda f: abs(f.ms - anchor_ms))             # earliest on tie
    delta = chosen.ms - anchor_ms
    return (chosen.stem, delta) if abs(delta) <= cap_ms else None           # inclusive cap
# ms = floor(float(stem) * 1000 + 0.5)   # round-half-AWAY-from-zero, matching Swift .rounded()
# blocked_intervals indeterminate (fail-closed) -> caller passes a sentinel that makes eligible empty -> None
```

---

## Implementation Units

> U-IDs are stable. They are listed in numeric order; `**Dependencies:**` (not list position) define execution order — U2 depends on the blocked-interval reader U6, which appears later in the document.

### U1. Pure nearest-frame resolution core (port of Swift `FrameSelection` + blocked filter)

**Goal:** A standalone, destination-agnostic module that lists a recording's `screenshots/*.jpg`, parses stems to ms (round-half-away-from-zero), excludes frames inside passed-in blocked intervals, and returns the nearest eligible stem + signed delta within a staleness cap — matching the Swift selection semantics and never selecting a blocked frame.

**Requirements:** R2, R6, R8 (selection half), R7

**Dependencies:** None

**Files:**
- Create: `src/screencap/frame_resolve.py`
- Test: `tests/test_frame_resolve.py`

**Approach:**
- `load_frames(screenshots_dir) -> list[tuple[int, str]]`: `glob("*.jpg")` only; reuse `redaction.geometry.parse_screenshot_timestamp` for stem→float; `ms = int(math.floor(ts * 1000 + 0.5))` (away-from-zero, matches Swift `.rounded()`); stem = filename minus `.jpg`; sort ascending. Skip non-numeric stems.
- `nearest_frame(frames, timestamp_ms, staleness_cap_ms=30_000, blocked_intervals=()) -> tuple[str, int] | None`: drop frames whose ms falls in any blocked interval; absolute-nearest via `min(..., key=abs distance)` over the eligible set (earliest-wins on tie); inclusive `abs(delta) <= cap`; signed `delta`; empty eligible set → `None`.
- `resolve_nearest(recording_dir, timestamp_ms, staleness_cap_ms, blocked_intervals) -> tuple[str, int] | None`: composes `load_frames(recording_dir / "screenshots")` + `nearest_frame`; returns `None` when the screenshots dir is missing. **No `screencap.daemon` import; no recording.db read** — blocked intervals arrive as a parameter.

**Execution note:** Port from the *verified Swift code* (`RecordingFrameIndex.swift`), not the ticket prose. Write parity tests first.

**Technical design:** see High-Level Technical Design pseudo-code (directional).

**Patterns to follow:**
- `src/screencap/index_core.py:187`-`195` (glob/parse/sort) and its `SCRUB_BLOCK_ACTIONS` interval-skip shape.
- `src/screencap/redaction/geometry.py:38` (`parse_screenshot_timestamp`).
- Swift source of truth: `macos/ScreenCap/Controllers/RecordingFrameIndex.swift:26` (`nearest`), `:99` (`loadFrames`), `:65` (cap).

**Test scenarios:**
- Happy path — frames at epochs `[1719400000.0, 1719400001.25, 1719400002.5]`, anchor `1719400001000` ms → nearest stem + signed `delta_ms`; asserts parse+sort+ms-rounding.
- Happy path — exact-hit anchor → `delta_ms == 0`.
- Edge case — round-half-away-from-zero: a stem whose `ts*1000` lands exactly on `.5` ms rounds **up/away** (e.g. `…2500.5 → 2501`), proving divergence from Python `round()`'s banker's result. *(This fixture has no Swift counterpart; it establishes the Python rule directly.)*
- Edge case — anchor before first / after last frame both resolve to the boundary frame (absolute-nearest, not nearest-prior).
- Edge case — exact equidistant tie returns the **earlier** frame. *(Note: the Swift suite has no tie test; this asserts the Python `min`-over-ascending behavior, which coincides with Swift's strict-`<` predicate — it is a Python-established contract, not mirrored Swift fixture.)*
- Edge case — empty frame list → `None`; missing `screenshots/` dir → `resolve_nearest` returns `None` (no exception).
- Edge case — mixed extensions (`notanumber.jpg`, a `.png`, a `.jpeg`) all excluded; only numeric `.jpg` stems considered.
- **Blocked filter (R8)** — the otherwise-nearest frame falls inside a blocked interval → it is skipped and the nearest **eligible** frame is returned; if the only frames within cap are all blocked → `None`.
- **Blocked filter** — a sentinel "all-blocked" interval set → `None` regardless of frames (fail-closed path exercised at the core level).
- Error/miss — `abs(delta) == cap` resolves (inclusive); `cap + 1` → `None`.

**Verification:** `tests/test_frame_resolve.py` passes; stems/ms match the Swift `RecordingFrameIndexTests` values for shared fixtures; the half-ms and blocked-filter cases hold.

---

### U2. Daemon `POST /v0/frame.nearest` verb

**Goal:** Expose U1 (fed by U6's blocked intervals) as a read-only daemon verb with validated, bounded inputs, a versioned envelope, and the pointer-only miss contract.

**Requirements:** R1, R5, R6, R7, R8

**Dependencies:** U1, U6

**Files:**
- Modify: `src/screencap/daemon/app.py` (handler `frame_nearest`, thread helper `_run_frame_nearest`, route registration)
- Modify: `src/screencap/daemon/schema.py` (`_FRAME_NEAREST_API_VERSION`, `FrameNearestRequest`, `FrameNearestResponse`, registrations)
- Test: `tests/daemon/test_read_only_verbs.py`

**Approach:**
- `schema.py`: `_FRAME_NEAREST_API_VERSION = 1`; `FrameNearestRequest{recording: str, timestamp_ms: int, staleness_cap_ms: int = 30_000}` with bounds — `0 <= timestamp_ms <= MAX_EPOCH_MS`, `0 <= staleness_cap_ms <= 86_400_000`; `FrameNearestResponse{stem: str | None, delta_ms: int | None}`. Register in `_MODEL_NAMES`, `_MODELS`, `__all__`.
- `app.py`: `async def frame_nearest(request)` mirroring `transcript_search` — parse/validate, `validate_recording_name`, then `await asyncio.to_thread(_run_frame_nearest, ...)`. `_run_frame_nearest` resolves the dir (`config.resolve_recording_dir`), loads blocked intervals via U6 (fail-closed → all-blocked sentinel on indeterminate), and calls `frame_resolve.resolve_nearest(dir, timestamp_ms, staleness_cap_ms, blocked)`. Wrap in `schema.envelope(...)`. Miss → both null, `ok:true`.
- Register `Route("/v0/frame.nearest", frame_nearest, methods=["POST"])`; **do not** add to `_ACTIVITY_PATHS`.
- Typed-error path: malformed body / bad name / out-of-bounds input → `DaemonAPIError` → `_api_error_response` (4xx); internal faults → `_internal_error_response` (class-name-only). Re-read per request (R7).

**Patterns to follow:** `transcript_search` (`app.py:1121`), `timeline_query` (`app.py:1153`), route block `app.py:1320`-`1344`, `schema.envelope` + version-constant conventions.

**Test scenarios:**
- Happy path — recording with seeded `screenshots/<epoch>.jpg` + no blocked intervals; within-cap `timestamp_ms` → `ok:true`, nearest `stem`, signed `delta_ms`; `_assert_envelope(payload, expected_schema_version=1)`.
- **Blocked-frame miss (R8, privacy)** — nearest frame is inside a blocked interval seeded in recording.db → verb returns the nearest **ALLOW** stem, or null if all in-cap frames are blocked. Mark `@pytest.mark.privacy`.
- **Fail-closed (R8, privacy)** — recording.db absent/unreadable → verb returns `stem:null` (not a 500, not an unfiltered frame). Mark `@pytest.mark.privacy`.
- Edge/miss — over-cap, zero frames, or missing `screenshots/` → `ok:true`, nulls; no 500.
- Error path — traversal/invalid recording name → `ok:false`, `error == "invalid_name"`, 4xx. Mark `@pytest.mark.privacy`.
- Error path — missing `timestamp_ms`, negative or over-bound `staleness_cap_ms`, over-bound `timestamp_ms` → typed 4xx.
- Privacy — response text carries no `.jpg` and no path separators, only the bare stem. Mark `@pytest.mark.privacy`.

Keep all privacy-marked tests Vision/OCR-free (naturally satisfied — no OCR in this path).

**Verification:** new verb tests pass; full `tests/daemon/test_read_only_verbs.py` green; verb absent from `_ACTIVITY_PATHS`.

---

### U3. Enrich `transcript.search` hits for chunk-granular frame resolution

**Goal:** Add `timestamp_ms` (chunk_start), `timestamp_granularity: "chunk"`, and `chunk_duration_ms` to each `transcript.search` hit so an agent can resolve a representative chunk frame with a chunk-scaled cap and label it honestly as coarse.

**Requirements:** R4, R9

**Dependencies:** None *(independent of U1/U6; shares no code with the frame core)*

**Files:**
- Modify: `src/screencap/daemon/app.py` (`_run_transcript_search` + a small `_chunk_timing` helper)
- Modify: `src/screencap/daemon/schema.py` (transcript-hit model gains the three nullable fields; consider `_TRANSCRIPT_SEARCH_API_VERSION` bump)
- Test: `tests/daemon/test_read_only_verbs.py`

**Approach:**
- `_chunk_timing(recording_dir, chunk_index) -> tuple[int, int] | None`: read `chunk_{chunk_index:04d}_manifest.json`; return `(round_away(chunk_start*1000), round_away((chunk_end-chunk_start)*1000))`; `None` if missing/unreadable/incomplete. Use the same away-from-zero rounding as U1 so the cross-verb anchor is consistent. Local-only read; manifest carries only counts/timing/blocked-intervals (no OCR/URL) — R6-safe.
- Wire into `_run_transcript_search` (`app.py:816`): per hit attach `timestamp_ms`, `chunk_duration_ms` (nullable), and constant `timestamp_granularity="chunk"`. Never fail the search when a manifest is absent.
- `schema.py`: add `timestamp_ms: int | None`, `chunk_duration_ms: int | None`, `timestamp_granularity: str | None` to the transcript-hit model.

**Patterns to follow:** `_generate_manifest_v2` (`task_manifest.py:61`) for field names; `_parse_chunk_index` (`app.py:806`); nullable-contract discipline from the review-data-nullable-timing learning.

**Test scenarios:**
- Happy path — hit whose chunk has a manifest → `timestamp_ms == round_away(chunk_start*1000)`, `chunk_duration_ms == round_away((end-start)*1000)`, `timestamp_granularity == "chunk"`.
- Edge case — manifest absent/unreadable → hit still returned with `timestamp_ms: null`, `chunk_duration_ms: null`; search does not error.
- Integration (R9) — feed a transcript hit's `timestamp_ms` into `frame.nearest` with `staleness_cap_ms = chunk_duration_ms`, on a recording where the matched word is mid-chunk (>30s from chunk_start): it **resolves** (because the cap is chunk-scaled), and the same call with the **30s default** cap **misses** — pinning that the chunk-granular contract is what makes it work and that the 30s default is intentionally insufficient here.
- Edge case — sub-ms `chunk_start` rounds away-from-zero identically to U1 so the cross-verb anchor agrees.

**Verification:** transcript-search tests pass with the new fields; existing transcript tests updated for any schema-version bump; the R9 integration scenario resolves with the chunk cap and misses with the default.

---

### U4. MCP surface: `frame.nearest` tool + client method + transcript field passthrough

**Goal:** Surface the verb as an MCP tool and propagate the new transcript fields through the MCP layer, with a docstring that states the stem is ALLOW-filtered, pointer-only, and (for transcript anchors) chunk-granular.

**Requirements:** R3, R4, R9

**Dependencies:** U2, U3

**Files:**
- Modify: `src/screencap/mcp/_client.py` (`frame_nearest` client method)
- Modify: `src/screencap/mcp/server.py` (`resolve_frame` tool + `FrameNearest` model; add `timestamp_ms`/`chunk_duration_ms`/`timestamp_granularity` to `TranscriptHit`; register in `build_server`)
- Test: `tests/test_mcp_server.py`

**Approach:**
- `_client.py`: `async def frame_nearest(self, recording, timestamp_ms, *, staleness_cap_ms=None)` posting to `/v0/frame.nearest`; mirror `transcript_search` (`_client.py:112`).
- `server.py`: model `FrameNearest{stem: str | None, delta_ms: int | None}`; tool `async def resolve_frame(recording, timestamp_ms, staleness_cap_ms=None) -> FrameNearest` forwarding through the client. Docstring states: returns a pointer-only stem of an **ALLOW** frame (masked/excluded frames are filtered out); the agent builds the path; for transcript anchors pass `staleness_cap_ms = chunk_duration_ms` and treat the result as chunk-granular (`delta_ms` is offset-from-chunk-start, not from the matched word). Add to the `build_server()` tuple. Extend `TranscriptHit` (`server.py:61`) with the three new nullable fields and map them through `search_transcript`.
- Liveness inherited via the shared client's `/v0/events` subscription — no change.

**Patterns to follow:** `search_transcript` (`server.py:180`), `_StubClient` + `_use_client` (`tests/test_mcp_server.py:27`, `:55`), `test_transcript_and_timeline_tools_map` (`:84`), in-process round-trip `_test_daemon_client()` (`:280`).

**Test scenarios:**
- Happy path — `resolve_frame` maps the envelope → `FrameNearest(stem=..., delta_ms=...)` (stub client).
- Edge/miss — daemon nulls → `FrameNearest(stem=None, delta_ms=None)`.
- Integration — in-process round-trip (`AsyncDaemonClient` over `ASGITransport(build_app())`) with a seeded recording (incl. a blocked interval) → `resolve_frame` returns the nearest **ALLOW** stem.
- Happy path — `search_transcript` surfaces `timestamp_ms`, `chunk_duration_ms`, `timestamp_granularity` on `TranscriptHit` (nulls tolerated).

**Verification:** `tests/test_mcp_server.py` green; the tool appears in `build_server`'s registered set; round-trip resolves a real ALLOW frame.

---

### U5. Docs: document the resolution primitive

**Goal:** Document the verb + MCP tool, the ALLOW-only filtering guarantee, and the chunk-granular transcript contract.

**Requirements:** R1, R3, R4, R8, R9

**Dependencies:** U2, U3, U4

**Files:**
- Modify: `docs/mcp-client-setup.md` (new `resolve_frame` tool; the search-hit → nearest-frame loop; stem-not-bytes; ALLOW-only filtering; chunk-granular transcript usage with `chunk_duration_ms`)
- Modify: `CLAUDE.md` (one line under "Daemon query verbs" for `/v0/frame.nearest` + the transcript enrichment fields)
- Consider: a `SECURITY.md` line noting `frame.nearest` applies the same `SCRUB_BLOCK_ACTIONS` skip as the content index, so the ALLOW-only invariant holds at the resolution layer.

**Approach:** Prose only; inputs/outputs, miss shape, privacy posture (pointer-only, ALLOW-only, no image bytes), and the chunk-granular caveat (`delta_ms` is from chunk start).

**Test scenarios:** Test expectation: none — documentation only.

**Verification:** docs match the shipped signatures and the ALLOW-only + chunk-granular contracts; `CLAUDE.md` query-verb list includes `frame.nearest`.

---

### U6. Blocked-interval reader for a recording (recording.db, fail-closed)

**Goal:** Provide the daemon adapter with a recording's `SCRUB_BLOCK_ACTIONS` blocked intervals (ms ranges) so U2 can feed them into U1's filter — reusing the existing fail-closed skip-interval derivation rather than re-deriving privacy logic.

**Requirements:** R8, R7

**Dependencies:** None *(consumed by U2)*

**Files:**
- Modify (or thin wrapper): `src/screencap/daemon/app.py` helper `_blocked_intervals_ms(recording_dir) -> BlockedSet`, delegating to `backfill/skip_intervals.py`
- Possibly modify: `src/screencap/backfill/skip_intervals.py` (only if a directly-callable "blocked intervals for recording" entry point must be extracted from existing logic — do not duplicate)
- Test: `tests/daemon/test_read_only_verbs.py` (or a focused `tests/test_blocked_intervals_reader.py`)

**Approach:**
- Reuse `backfill/skip_intervals.py`'s recording.db-backed, `PrivacyMode`-aware, fail-closed derivation over the intact local `recording.db` to produce blocked ms-intervals for the recording. The `backfill` package does not import `screencap.daemon`, so the daemon importing it is cycle-free.
- **Fail-closed contract:** missing/unreadable `recording.db`, deleted-row coverage gap, or classification ambiguity → return an "all-blocked" sentinel (or raise a sentinel the adapter maps to all-blocked) so U1's eligible set is empty and `frame.nearest` returns a miss. Never return an empty/permissive set on uncertainty.
- Re-read per request (R7) — no caching of blocked geometry at daemon launch.

**Execution note:** Privacy-critical. Prefer importing/reusing the existing skip logic over re-implementing; if extraction is needed, keep the fail-closed branches intact and characterization-test them before refactoring.

**Patterns to follow:** `src/screencap/backfill/skip_intervals.py` (fail-closed derivation, `PrivacyMode` per `.recording_intent`); `src/screencap/index_core.py` `SCRUB_BLOCK_ACTIONS` usage.

**Test scenarios:**
- Happy path — recording.db with a known blocked window → reader returns that window as a ms-interval covering the expected frames. Mark `@pytest.mark.privacy`.
- Fail-closed — missing/unreadable recording.db → all-blocked sentinel (downstream miss). Mark `@pytest.mark.privacy`.
- Fail-closed — deleted-row coverage gap / null-column classification ambiguity → all-blocked sentinel. Mark `@pytest.mark.privacy`.
- Happy path — fully-ALLOW recording → empty blocked set (everything eligible).

**Verification:** reader returns correct intervals for clear cases and the all-blocked sentinel for every ambiguous/error case; reused skip logic is not forked.

---

## System-Wide Impact

- **Interaction graph:** Adds one read-only HTTP route + one MCP tool, and a new read of `recording.db` blocked geometry (via reused backfill skip logic) on the resolution path. Enriches `transcript_search`'s response (three additive fields). No write paths, no recording lifecycle, no event bus.
- **Error propagation:** Bad/out-of-bounds input → typed `DaemonAPIError` → 4xx; misses (over-cap / no eligible frames / indeterminate blocked geometry) → `ok:true` nulls; internal faults → `_internal_error_response` (class-name-only). The MCP `_post`/`_ok` raises `DaemonError` on non-`ok`/≥400 — the tool surfaces it.
- **State lifecycle risks:** None persistent. R7 forbids daemon-launch caching of frames or blocked geometry; each request re-reads both, so recordings that grow or get retroactively scrubbed after launch resolve correctly.
- **API surface parity:** The verb mirrors the three `/v0/*.search` verbs (POST, validated, bounded, enveloped, pointer-only, out of `_ACTIVITY_PATHS`). The MCP tool mirrors the five existing tools. `transcript.search` schema gains additive nullable fields.
- **Integration coverage:** The R9 chunk-cap-resolves-vs-30s-default-misses scenario, the blocked-frame ALLOW-only filtering, the fail-closed path, and the MCP in-process round-trip are the behaviors unit-level stubs alone won't prove — all enumerated in U2/U3/U4/U6.
- **Unchanged invariants:** priv-R8 pointer-only holds (no image bytes ever cross the wire); the content index's ALLOW-only guarantee now holds at the resolution layer too (R8); `recording.db` is read-only here and never uploaded; the rich per-word transcript JSON is still never read; the Swift app-side resolver is unchanged.

---

## Risks & Dependencies

| Risk | Mitigation |
|------|------------|
| Python port diverges from Swift at the rounding / tie / cap boundary | U1 parity tests mirror the Swift fixtures **and** add a half-ms fixture that exercises round-half-away-from-zero (`floor(x+0.5)`), the exact case Swift fixtures miss. Tie + cap-inclusive cases are explicit. |
| `parse_screenshot_timestamp` accepts `.jpeg` but Swift lists `.jpg` only → frame-set mismatch | Constrain the glob to `*.jpg`; mixed-extension exclusion test in U1. |
| Blocked-interval logic forked from backfill → privacy drift between index-skip and resolve-skip | U6 reuses `backfill/skip_intervals.py` rather than re-deriving; if extraction is needed, characterization-test the fail-closed branches first. |
| Fail-open mistake leaks a masked frame on indeterminate blocked geometry | R8 fail-closed: any uncertainty → all-blocked sentinel → miss. Two explicit fail-closed tests in U6 + one in U2. |
| Agent mistakes a chunk-granular transcript frame for the exact matched-word frame | R9: `timestamp_granularity: "chunk"` field + MCP docstring state the anchor is chunk_start and `delta_ms` is offset-from-chunk-start; integration test pins the coarse behavior. |
| Long-lived daemon serves stale frames or stale blocked geometry | R7: re-read frames + blocked intervals per request; no module-level cache. |
| Additive transcript schema bump breaks existing schema-version test assertions | U3 deferred-impl note: confirm which `test_read_only_verbs.py` assertions pin the version and update them with the bump. |
| Unbounded `staleness_cap_ms` / `timestamp_ms` enumeration or arithmetic abuse | R5 upper bounds at the Pydantic layer (24h cap; max-epoch-ms); `load_frames` bounded by recording size (not truncated). |

---

## Sources & References

- **Origin ticket:** [SCR-186](https://linear.app/zk-email/issue/SCR-186/p2-add-daemon-v0-mcp-frameresolve-nearest-frame-primitive-scr-177) — related to [SCR-177](https://linear.app/zk-email/issue/SCR-177/search-results-screenshot-thumbnails-matched-text-highlighting) (PR [#288](https://github.com/proteus-computer-use/screencap/pull/288)).
- Swift algorithm: `macos/ScreenCap/Controllers/RecordingFrameIndex.swift`, tests `macos/ScreenCapTests/RecordingFrameIndexTests.swift`.
- Daemon: `src/screencap/daemon/app.py`, `schema.py`, `_idle_shutdown.py`, `_name_validation.py`.
- MCP: `src/screencap/mcp/server.py`, `src/screencap/mcp/_client.py`.
- Reuse: `src/screencap/redaction/geometry.py` (`parse_screenshot_timestamp`), `src/screencap/index_core.py`, `src/screencap/backfill/skip_intervals.py` (fail-closed blocked-interval derivation), `src/screencap/config.py` (`resolve_recording_dir`, `get_chunk_duration`).
- Chunk timing: `src/screencap/task_manifest.py` (`chunk_{idx:04d}_manifest.json`).
- Tests to mirror: `tests/daemon/test_read_only_verbs.py`, `tests/test_mcp_server.py`, `tests/test_index_core.py` (privacy-marker example).
- Privacy posture: `SECURITY.md` (ALLOW-only content-index invariant, SCR-118; per-query caps), `CLAUDE.md` ("Daemon query verbs").
