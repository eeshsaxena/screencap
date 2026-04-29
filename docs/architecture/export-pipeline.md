# Export Pipeline

## What it does

Converts raw events stored in SQLite into the `events.jsonl` format that downstream tools (viewer, scrubber, Cloud Run) consume. **Three callers, one callable.**

## Three callers, one callable

All three export callers route through the same pure transform — `unified_export_events` in `src/screencap/engine/export.py`. The callable is the single source of truth for the row-to-Pydantic-event pipeline; a fix to any pipeline stage applies once and propagates everywhere.

```
                       raw DB rows (action_event + window_event)
                                    │
                                    ▼
                       dict_to_action_event / dict_to_window_switch
                                    │
                                    ▼
                       process_events() — 11-stage merge pipeline
                                    │
                                    ▼
                       deduplicate_window_events()
                                    │
                                    ▼  (optional window_filter applied here)
                       interleave_window_events()
                                    │
                                    ▼
                       Iterator[BaseEvent]   ◄── unified_export_events
                                    │           (engine layer; pure;
                                    │            no DB, no file I/O)
                ┌───────────────────┼────────────────────┐
                │                   │                    │
                ▼                   ▼                    ▼
        screencap export    chunk_processor      _recover_chunk_metadata
        (CLI command)       (per-chunk)          (upload-time recovery)
                │                   │                    │
                │                   ▼                    ▼
                │            write_events_jsonl   write_events_jsonl
                │            (atomic .tmp +       (atomic .tmp +
                │             os.rename)           os.rename)
                ▼
        _write_events
        (atomic write
         or stdout)
```

The callable is **pure**: dict rows in, sorted Pydantic events out as an `Iterator[BaseEvent]`. It performs no DB access, no file I/O, and no privacy semantics for mouse coordinates — coordinate suppression lives at the scrub layer (see [scrubbing.md](./scrubbing.md) and the dedicated section below). Each caller is responsible for:

- Row fetching and the `disabled` filter.
- `initial_window_row` timestamp rewrite to `start_ts - 0.001` (so the synthetic event sorts before the chunk's first event).
- `mouse.move` dropping when desired (e.g., the CLI's `--exclude-moves` flag).
- Privacy-aware `window_filter` construction via `build_cloud_window_filter` for cloud-bound paths.
- `_meta` header generation and atomic file writes.

### Caller 1 — `screencap export` (full recording)

Lives in `screencap/exporter.py`. Used by the CLI `export` command.

Path: `Capture.load(dir)` → `CaptureSession.export_events(include_moves)` → `unified_export_events` (with `window_filter=None` and `initial_window_row=None` — full-recording exports do not prepend pre-chunk window context) → materialized to a `list[BaseEvent]` (preserves the `tests/test_cross_layer_contracts.py` public ORM contract). `export_recording` then wraps the list with the `_meta` header and writes JSONL via the existing `_write_events` helper.

> **No privacy filter on this path.** `export_recording` and `_export_one` pass no `window_filter`. CLI export emits events as captured. If the recording was made with cloud intent, capture-time enforcement and post-hoc scrubbing are the privacy guarantees, not export-time filtering.

Atomic write: `output.tmp` → `os.rename(output)`. The CLI also supports stdout when `output_path is None`. CLI keeps its existing `_write_events` helper rather than migrating to `write_events_jsonl` because `_write_events` already handles the stdout path.

### Caller 2 — chunk processor (per-chunk during recording)

Lives in `screencap/chunk_processor.py`. Used during cloud-intent recordings. Triggered on each chunk rotation.

Path: raw `sqlite3` queries (read-only, `PRAGMA query_only=ON`):

```sql
-- chunk events
SELECT * FROM action_event
WHERE timestamp >= ? AND timestamp < ?
  AND (disabled IS NULL OR NOT disabled)
ORDER BY timestamp;

SELECT * FROM window_event
WHERE timestamp >= ? AND timestamp < ?
  AND (disabled IS NULL OR NOT disabled)
ORDER BY timestamp;

-- initial window context (the last switch BEFORE the chunk's start_ts)
SELECT * FROM window_event WHERE timestamp < ? ORDER BY timestamp DESC LIMIT 1;
```

The initial window event has its timestamp rewritten to `start_ts - 0.001` so it sorts before the first chunk event. This guarantees the first action in the chunk has a known window context even if no window switch happened during the chunk.

Then calls `unified_export_events` with the cloud window filter constructed unconditionally via `build_cloud_window_filter(self._cloud_intent, ...)` (the factory returns `None` when `cloud_intent=False`; see "build_cloud_window_filter" below) and writes via `write_events_jsonl` (atomic `.tmp` + `os.rename`).

### Caller 3 — manual upload recovery (`_recover_chunk_metadata`)

Lives in `screencap/cli.py:_recover_chunk_metadata`, invoked by `screencap upload` when chunk videos exist on disk but `events_NNNN.jsonl` files are missing (e.g., the `ChunkProcessor` thread crashed during recording but media files were already written).

**Recovery now produces the same v2 format as the chunk processor.** Per-chunk recovery runs the identical fetch → unified callable → atomic write sequence:

1. Action SELECT with `(disabled IS NULL OR NOT disabled)` filter.
2. Window SELECT with the same `disabled` filter.
3. Initial-window-context SELECT (`timestamp < start_ts ORDER BY timestamp DESC LIMIT 1`); timestamp rewritten to `start_ts - 0.001`.
4. `window_filter = build_cloud_window_filter(cloud_bound, privacy_mode, capture_dir)` — wired unconditionally via the sanctioned factory.
5. `unified_export_events(...)`.
6. `write_events_jsonl(...)` — atomic write.

**Skip-on-error:** if `unified_export_events` raises mid-chunk on a corrupt row that trips a downstream stage, recovery logs a warning and skips that chunk's `events_NNNN.jsonl` (no file written). Other chunks proceed; `screencap upload --force` re-runs once the underlying data is fixed. Recovery's previous "raw dump tolerates everything" behaviour is intentionally retired.

#### `cloud_bound` is a REQUIRED keyword

`_recover_chunk_metadata(..., *, force: bool = False, cloud_bound: bool)` — the `cloud_bound` keyword has no default. Omitting it raises `TypeError` at the call site rather than silently falling open. The only caller today (`screencap upload`, `cli.py:1768`) passes `cloud_bound=True` unconditionally — at upload time the data IS becoming cloud-bound by user choice, regardless of what `.recording_intent` records. This closes the local-then-uploaded threat case (a recording captured as `destination=local` and later uploaded would otherwise have `.recording_intent` say "local" → recovery would skip the filter → window titles leak in the uploaded copy). See "Recovery's privacy posture" below for the trust-anchor rationale.

#### Recovery vs chunk-processor format parity

| Aspect | Chunk processor (Caller 2) | Recovery (Caller 3) |
|---|---|---|
| `_meta` header line | yes | **yes** |
| Window switch events | yes (deduplicated, interleaved) | **yes** (deduplicated, interleaved) |
| Pydantic event types (`mouse.singleclick`, `key.type`, `mouse.drag`, …) | yes | **yes** (via the unified callable) |
| 11-stage processing | yes | **yes** |
| Initial window context | yes | **yes** |
| Privacy filter on window switches (cloud-bound) | yes | **yes** (applied unconditionally for `cloud_bound=True`) |
| Atomic write (`.tmp` + `os.rename`) | yes | **yes** (via `write_events_jsonl`) |
| `disabled` row filter at SELECT layer | yes | **yes** |

Recovery output is byte-identical post-`_meta` to chunk-processor output for the same time range, modulo the meta header's `exported_at` timestamp — pinned by `tests/test_unified_export_contract.py`.

## events.jsonl format v2

```jsonl
{"_meta": true, "format_version": 2, "screencap_version": "0.18.0", "exported_at": "2026-...", "exclude_moves": false}
{"timestamp": 1700000000.123, "type": "window.switch", "app_name": "Visual Studio Code", "app_bundle_id": "com.microsoft.VSCode", "window_title": "<...>", ...}
{"timestamp": 1700000000.456, "type": "mouse.singleclick", "x": 100, "y": 200, "button": "left", "children": [...]}
{"timestamp": 1700000000.789, "type": "key.type", "text": "hello", "children": [...]}
...
```

- **Line 1** is always the `_meta` dict (plain `json.dumps`). All three callers emit it via `build_export_metadata`.
- **Subsequent lines** are Pydantic events serialized via `.model_dump_json()`.
- Events are time-ordered.
- **`mouse.move` defaults differ by caller**:
  - **CLI `screencap export`** uses `--exclude-moves` (default `False`), so moves are **included** by default. Pass `--exclude-moves` to drop them.
  - **Chunk processor** also keeps `mouse.move` by default. The pre-refactor behavior of dropping moves unconditionally was reverted after a measured audit (chunk JSONL size, scrubber walk wall time, end-to-end wall time, and Cloud Run parse RSS) confirmed bloat thresholds were satisfied. Cloud Run filters moves at iteration time; the scrub layer drops in-interval moves for sensitive contexts (see "Scrub-layer pointer suppression" below).
  - **Recovery** also keeps `mouse.move` by default — recovery now uses the same pipeline as the chunk processor.

## Privacy-aware window switches

The window filter constructor lives at `src/screencap/privacy/filter.py`. Its inner closure:

1. Fail-closed on null/empty/whitespace `app_bundle_id`. macOS accessibility sometimes fires a window event before `bundle_id` is resolved; without a bundle_id we cannot classify the app, so suppress the event rather than leak the original title.
2. Checks `.menubar_overrides.json` for runtime user toggles. `"exclude"` → return `None` (suppressed). `"allow"` → return event unchanged.
3. Otherwise runs `DefaultContextClassifier.classify` + `DefaultPolicyEvaluator.evaluate`.
4. Applies the resulting `PrivacyAction`:
   - **EXCLUDE** → return `None`. Event suppressed entirely.
   - **MASK_WINDOW** → return `event.model_copy(update={"window_title": event.app_name, "domain": None})`. Title becomes app name; domain nulled.
   - **TEXT_REDACT, ALLOW, others** → return event unchanged. Scrubbing of `key.type` text is deferred to the scrub pipeline.
5. If `cloud_intent=True`, mode is forced to `PrivacyMode.PUBLIC` regardless of the configured `privacy_mode`.

The cloud-bound chunk processor and recovery both pass this filter into `unified_export_events`. The full-recording CLI export path passes `window_filter=None` (capture-time enforcement + post-hoc scrubbing are the cloud-safety guarantees on the CLI path).

## `build_cloud_window_filter` and `build_local_window_filter` — sanctioned constructors

Both factories live in `src/screencap/privacy/filter.py`. They are the only entry points production code may use to construct a window-event filter.

- `build_cloud_window_filter(cloud_bound: bool, privacy_mode, capture_dir) -> Callable | None` — the cloud-bound entry point.
  - When `cloud_bound=False`, returns `None`.
  - When `cloud_bound=True`, returns the cloud-mode filter (built with `cloud_intent=True`, which forces PUBLIC mode regardless of configured `privacy_mode`).
- `build_local_window_filter(privacy_mode, capture_dir) -> Callable` — the local-only entry point used by the CLI `export --privacy-filter` path. Always returns a filter (no `cloud_bound`-style structural switch); the caller has already decided to apply privacy filtering by calling this constructor. Builds with `cloud_intent=False`, which honors the configured `privacy_mode` and consults `.menubar_overrides.json`.

The cloud factory's shape is deliberate: returning `None` for the non-cloud path lets call sites wire the factory **unconditionally** — `window_filter=build_cloud_window_filter(self._cloud_intent, ...)` — eliminating the `if cloud_intent: build_filter() else None` pattern. That conditional pattern is exactly the failure mode that produced the prior Slack-title leak (a path that bypassed the filter when reviewer vigilance lapsed); removing the conditional removes the failure mode.

Defense-in-depth: `tests/test_privacy_filter_call_graph.py` is a static AST walker (CI guard) that scans `src/screencap/`. It enforces four rules:

1. `build_privacy_filter` is imported only from `privacy/filter.py` (declaration site) and the `exporter.py` backward-compat re-export. Any other import path within `src/screencap/` fails the test.
2. Every call to `unified_export_events(...)` from a cloud-bound file (every file under `src/screencap/` except `engine/capture.py`, the CLI-export site) MUST explicitly pass a `window_filter=` kwarg, AND that value must not be a literal `None`. Two regression vectors are caught: (a) the omitted-kwarg case (`unified_export_events(rows, windows)` falls through to the function default `window_filter=None` and silently leaks titles), and (b) the literal `window_filter=None` case (a deliberate or careless bypass of `build_cloud_window_filter`). CLI export's `CaptureSession.export_events` (`src/screencap/engine/capture.py`) is the only legitimate `window_filter=None` site and is exempted by file path.
3. `build_privacy_filter` is module-private to `privacy/filter.py`. Any direct call from any other file under `src/screencap/` fails the test, regardless of arguments. The two sanctioned constructors above (`build_cloud_window_filter` and `build_local_window_filter`) are the only production entry points. The exporter re-export remains for backward-compat *imports* (test files depend on it), but production code must not invoke the symbol — going through a factory means filter construction stays centralized in one place with one set of pre-flight checks.
4. Legacy literal-`cloud_intent=False` check, preserved alongside (3) for defense in depth: a literal `False` constant in the `cloud_intent` kwarg position from outside the factory home is also flagged. With (3) in place this is moot for production code, but the helper machinery is exercised by sensitivity self-tests that pin the original Unit 8 enforcement contract.

**Known limitation: the AST walker matches direct name and attribute references only.** Local aliasing defeats the matcher silently:

- **Local `as` aliasing.** `from screencap.engine.export import unified_export_events as fn` then `fn(rows, windows, window_filter=None)`. The walker sees `Call(func=Name("fn"), ...)`, not `unified_export_events`, and the call escapes detection.
- **Variable-assignment aliasing.** `_alias = unified_export_events` followed by `_alias(...)`. Same blind spot.
- **Dynamic imports / `getattr` lookups.** `importlib.import_module("screencap.engine.export").unified_export_events(...)` or `getattr(mod, "unified_export_events")(...)`. The symbol name appears only as a string literal, invisible to AST-name matching.
- **Logic copy-paste.** A future caller could open-code the `DefaultPolicyEvaluator` + `DefaultContextClassifier` closure pattern inline. The Slack-leak prior incident was exactly this shape.

These vectors are mitigated by code review; this document names `build_cloud_window_filter` as the only sanctioned constructor. A reviewer who sees an alias or a dynamic lookup of either `unified_export_events` or `build_privacy_filter` in a new code path should treat it as a warning sign and reject the change unless the call site goes through the factory.

## Recovery's privacy posture — call-site context, not the intent file

Recovery's `cloud_bound` parameter is the trust anchor, not `.recording_intent`. The reasoning:

- The threat case the intent-file mechanism misses is the **local-then-uploaded** scenario: a recording started as `destination=local`, later uploaded via `screencap upload`. Intent file says "local" → if recovery consulted only the intent file, it would skip the filter → window titles would leak in the uploaded copy. Passing `cloud_bound=True` from the upload command unconditionally closes this.
- `screencap upload` is the only caller today, and at the moment it runs the data IS becoming cloud-bound regardless of intent file contents.
- The required-keyword shape (`cloud_bound` has no default) makes "forgot to pass it" a `TypeError` at the call site, not a silent fail-OPEN.

The `.recording_intent` mechanism is preserved as documentation of the **fallback for hypothetical future non-upload recovery contexts** (none exist today). If such a context is added, it should derive `cloud_bound = (read_intent(capture_dir) != "local")` — treating any other value (`None`, `"cloud"`, `"both"`, parse error, type mismatch, case mismatch) as cloud-bound. Recovery's privacy posture is fail-safe-to-filtered.

## Scrub-layer pointer suppression in sensitive intervals

Capture-time enforcement and the cloud window filter handle window-title and screenshot privacy. Mouse-coordinate geometry inside sensitive intervals is handled at the **scrub layer**, not the engine callable.

The scrubber drops `mouse.move` events whose timestamp falls inside an interval whose privacy action is in `SCRUB_BLOCK_ACTIONS` (defined in `src/screencap/privacy/actions.py`):

```python
SCRUB_BLOCK_ACTIONS = frozenset({
    PrivacyAction.EXCLUDE,
    PrivacyAction.MASK_WINDOW,
    PrivacyAction.TEXT_REDACT,
    PrivacyAction.OCR_FALLBACK,
})
```

This is broader than `BLOCK_ACTIONS` (screenshot capture-time) on purpose: TEXT_REDACT covers code editors and admin consoles under PUBLIC mode; OCR_FALLBACK covers unverified browsers under SHARED mode. Both contexts have content-sensitive matrix routing, so pointer geometry inside them is comparably sensitive (which terminal line was being edited; which credentials field was being hovered; browsing patterns inside unverified tabs).

Drag children (`MouseDragEvent.children`) containing inline `mouse.move` entries are also dropped via the same predicate. The drag event itself is retained.

Engine-layer keystroke nulling and screenshot redaction continue to use their existing action-set constants (`KEYSTROKE_NULL_ACTIONS`, `VIDEO_BLOCK_ACTIONS`); `SCRUB_BLOCK_ACTIONS` is a fourth, scrub-time-only constant for pointer geometry. See [scrubbing.md](./scrubbing.md) for the full scrub pipeline.

## Window switch deduplication

`deduplicate_window_events()` collapses raw window event rows on `(app_bundle_id, window_id)`. Title-only changes within the same window do NOT trigger a new switch event. A late-arriving `browser_url` (common with async AX URL extraction) patches the `domain` of the already-emitted event via `model_copy()`.

## Load-bearing invariants

- **First line is `_meta` for ALL THREE callers.** Recovery now matches chunk processor and CLI export — the historical recovery exception (raw row dump, no header) is retired. Parsers should still check `data.get("_meta")` to identify the header for forward-compat, but the absent-header case no longer occurs in practice for files written by post-refactor code.
- **`format_version: 2` is current.** v1 manifests existed historically; v2 is the live format. Bumping requires updating Cloud Run's manifest routing too.
- **Atomic write for ALL THREE callers.** CLI export (`_write_events`) and the shared `write_events_jsonl` helper (chunk + recovery) both use `.tmp` + `os.rename`. The historical "recovery writes non-atomically" exception is retired. Crash mid-write leaves no partial JSONL — the `.tmp` is unlinked in a `BaseException` handler. The `force` flag on `screencap upload` re-runs recovery for files that were never finalized.
- **Privacy filter only applies on the chunk and recovery paths when `cloud_bound`/`cloud_intent` is true.** CLI `screencap export` does not run a privacy filter. Cloud-safety on full export must come from capture-time enforcement (already-blocked content was never written) and post-hoc scrubbing.
- **Dedup on `(app_bundle_id, window_id)`, not title.** Title-only changes are noise.
- **Initial window context query timestamp rewrite is required.** `start_ts - 0.001` ensures the synthetic event sorts first. Removing the rewrite breaks the first-action window-context guarantee.
- **Cloud intent forces PUBLIC privacy mode.** Local recordings respect the configured mode. Don't assume the configured mode applies — check `cloud_intent`/`cloud_bound`.
- **MASK_WINDOW null both `window_title` AND `domain`.** Replacing only the title would still leak via `domain` for browsers.
- **Mouse.move handling diverges by caller.** All three callers keep moves by default post-refactor; CLI exposes `--exclude-moves` for opt-out. The chunk-processor default flip from "drop unconditionally" to "keep" was gated by a measured pre-merge audit (chunk JSONL size, scrubber walk wall time, end-to-end wall time, Cloud Run RSS thresholds — see the linked plan).
- **Chunk processor and recovery use raw `sqlite3`, not SQLAlchemy.** Loads faster, avoids ORM overhead, and `query_only=ON` is read-safe during writes.
- **`build_privacy_filter` reads `.menubar_overrides.json` at filter construction.** Subsequent menubar toggles do not affect an in-flight export.
- **Filter `None` return = suppress.** Don't return the unmodified event by mistake — it would defeat EXCLUDE.

### New load-bearing invariants introduced by the unified-callable refactor

- **Recovery → scrubber ordering.** `cli.py`'s upload command sequences `_recover_chunk_metadata(...)` → `scrub_recording(...)`. The scrub-layer pointer suppression in `SCRUB_BLOCK_ACTIONS` only protects recovered cloud-bound JSONL when this ordering holds. Any future refactor that reorders these calls or adds a recovery-only code path that bypasses the scrubber MUST replicate the in-interval `mouse.move` drop at the engine layer — otherwise the cloud-bound privacy posture silently degrades. The relevant call sites are documented inline at `cli.py:1762-1768` (recovery) and `cli.py:1782-1786` (scrub).
- **Cloud Run consumer no-raw-archive assumption.** The decision to keep `mouse.move` events in chunk JSONL by default assumes Cloud Run does NOT persist raw `events_*.jsonl` content to long-term storage before applying its iteration-time `mouse.move` filter (`scripts/process-recording/main.py:1102, 1562`). If Cloud Run ever archives raw chunk JSONL (e.g., for an ML training corpus or audit log), in-interval `mouse.move` coordinates from a local-then-uploaded recording could be persisted server-side. The mitigation is already wired today: `scrub_recording` runs before the upload step, so on-disk JSONL is suppression-applied. If a raw-archive path is ever added on the cloud side, the assumption documented here breaks and the suppression must move to capture time (or the engine layer).
- **Factory call-graph contract.** `build_cloud_window_filter` and `build_local_window_filter` are the two sanctioned constructors. Production callers must use one of them — `build_privacy_filter` is module-private (called only by the two factories and by tests). Enforced by the AST walker in `tests/test_privacy_filter_call_graph.py`. A future caller that imports `build_privacy_filter` directly from outside the factory home, or passes `window_filter=None` literally to `unified_export_events` from cloud-bound code, fails the build.

## Architectural debt acknowledged

The three load-bearing invariants above (recovery → scrubber ordering, Cloud Run consumer no-raw-archive assumption, factory call-graph contract) would be eliminated by capture-time pointer suppression in `src/screencap/privacy/recorder_enforcement.py`. Coordinates inside MASK_WINDOW intervals would never be written to `recording.db` in the first place, and downstream filter ordering would no longer matter.

The current scrub-layer placement was a deliberate **blast-radius choice** — the scrubber already runs on cloud-bound chunks today, while a capture-time enforcement change touches the live recording fast path. The user explicitly chose the smaller-blast-radius option for this refactor and accepted the temporary scaffolding (the load-bearing invariants).

A successor plan should retire this scaffolding by moving pointer suppression to capture-time. Without an explicit follow-up, the scaffolding hardens into "how the system works" and capture-time becomes harder to ship later. (No tracking ticket exists today; one should be opened.)

## Before you change it

- Bumping `format_version`: every consumer (viewer, scrubber, Cloud Run) must handle both versions or be updated in lockstep.
- Adding a field to the `_meta` header: `build_export_metadata` is the single place to change. Ensure consumers handle missing fields (older recordings will lack new keys).
- Changing the privacy filter for window switches: it ALSO runs at scrub time on `WindowSwitchEvent` text. Don't add side effects (e.g., logging) — runs on every event.
- Adding a new event type: the chunk processor's raw-sqlite3 query reads `action_event.*` flat — make sure the new type's columns are already in the schema. Otherwise migrate first.
- Changing the chunk time-range query: `[start_ts, end_ts)` is half-open; events at exactly `end_ts` belong to the next chunk. Test boundary handling.
- Removing `mouse.move` from raw capture entirely: would simplify export but break drag detection (stage 9 needs the moves to compute distance).
- Adding a fourth caller of `unified_export_events`: route cloud-bound construction through `build_cloud_window_filter`; the AST walker will fail the build if you don't.

## See also

- [event-system.md](./event-system.md) — the 11-stage pipeline applied here
- [database.md](./database.md) — raw rows + schema
- [privacy.md](./privacy.md) — what the privacy filter checks
- [scrubbing.md](./scrubbing.md) — post-export text scrubbing and pointer-suppression intervals
- [segmentation.md](./segmentation.md) — what the manifest carries alongside JSONL
- [network-capture.md](./network-capture.md) — `unified_export_events` gained a kw-only `network_rows` parameter in V1, but every caller passes `None` — JSONL emission of network events is V1.75-coupled with the cloud bucket policy + `build_cloud_network_filter` factory; V1 keeps DB-only emission. Three explicit scope-guard tests assert zero `network.*` lines in CLI export, chunk JSONL, and recovery JSONL.
- [Unified-callable refactor plan](../plans/2026-04-26-001-refactor-unified-export-callable-plan.md) — the plan that landed this structure (three callers, one callable; factory; scrub-layer pointer suppression; recovery format parity)
