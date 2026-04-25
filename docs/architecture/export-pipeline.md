# Export Pipeline

## What it does

Converts raw events stored in SQLite into the `events.jsonl` format that downstream tools (viewer, scrubber, Cloud Run) consume. Two callers, one shared processing chain.

## Two callers, one chain

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
                       drop MouseMoveEvent (default; --include-moves)
                                    │
                                    ▼
                       deduplicate_window_events()
                                    │
                                    ▼  (privacy filter on window switches)
                       interleave_window_events()
                                    │
                                    ▼
                       _meta header line + events as JSONL
                                    │
                ┌───────────────────┴────────────────────┐
                │                                        │
                ▼                                        ▼
        screencap export                      chunk_processor (per-chunk)
        (CLI command)                         events_NNNN.jsonl
        events.jsonl
        (full recording)
```

### Caller 1 — `screencap export` (full recording)

Lives in `screencap/exporter.py`. Used by the CLI `export` command.

Path: `CaptureSession.load(dir)` → `capture.export_events(include_moves)` → uses SQLAlchemy ORM to read all events, runs `process_events`, returns flat list. The exporter wraps with the `_meta` header and writes JSONL.

> **No privacy filter on this path.** `_write_events` accepts a `privacy_filter` kwarg, but `export_recording` and the CLI's `_export_one` never pass one. CLI export emits events as captured. If the recording was made with cloud intent, capture-time enforcement and post-hoc scrubbing are the privacy guarantees, not export-time filtering.

Atomic write: `output.tmp` → `os.rename(output)`.

### Caller 2 — chunk processor (per-chunk during recording)

Lives in `screencap/chunk_processor.py`. Used during cloud-intent recordings. Triggered on each chunk rotation.

This is the **only** path that produces v2-format `events_NNNN.jsonl` (with `_meta` header, processed events, deduplicated window switches, privacy filter applied).

Path: raw `sqlite3` queries (read-only, `PRAGMA query_only=ON`):

```sql
-- chunk events
SELECT * FROM action_event WHERE timestamp >= ? AND timestamp < ? ORDER BY timestamp;
SELECT * FROM window_event WHERE timestamp >= ? AND timestamp < ? ORDER BY timestamp;

-- initial window context (the last switch BEFORE the chunk's start_ts)
SELECT * FROM window_event WHERE timestamp < ? ORDER BY timestamp DESC LIMIT 1;
```

The initial window event has its timestamp rewritten to `start_ts - 0.001` so it sorts before the first chunk event. This guarantees the first action in the chunk has a known window context even if no window switch happened during the chunk.

Then runs the same shared chain. Atomic write: `events_NNNN.jsonl.tmp` → `os.rename`.

### Caller 3 — manual upload recovery (`_recover_chunk_metadata`)

Lives in `screencap/cli.py:_recover_chunk_metadata`, invoked by `screencap upload` when chunk videos exist on disk but `events_NNNN.jsonl` files are missing (e.g., the `ChunkProcessor` thread crashed during recording but media files were already written).

**This path produces a different format from Caller 2.** It writes raw `action_event` rows directly:

```python
rows = conn.execute(
    "SELECT * FROM action_event WHERE timestamp >= ? AND timestamp < ? ORDER BY timestamp",
    (c_start, c_end),
).fetchall()
with open(jsonl_path, "w") as f:
    for row in rows:
        f.write(json.dumps(dict(row)) + "\n")
```

Differences vs the v2 format:

| Aspect | v2 (chunk processor) | Recovery (`_recover_chunk_metadata`) |
|---|---|---|
| `_meta` header line | yes | **no** |
| Window switch events | yes (deduplicated, interleaved) | **no — only `action_event` rows** |
| Pydantic event types (`mouse.singleclick`, `key.type`, `mouse.drag`, …) | yes | **no — raw rows with `name = "click"`/`"press"`/etc.** |
| 11-stage processing | yes | **no** |
| Privacy filter on window switches | yes | **n/a (no window events at all)** |
| Initial window context | yes | no |
| Atomic write (`.tmp` + `os.rename`) | yes | **no — direct write** |

Cloud Run's LLM activity summary depends on `window.switch` entries, so recovered chunks degrade the segmentation quality — the LLM sees no app or window context. Treat this as a fallback for partially-failed recordings, not as a substitute for the chunk processor's output.

## events.jsonl format v2

```jsonl
{"_meta": true, "format_version": 2, "screencap_version": "0.18.0", "exported_at": "2026-...", "exclude_moves": true}
{"timestamp": 1700000000.123, "type": "window.switch", "app_name": "Visual Studio Code", "app_bundle_id": "com.microsoft.VSCode", "window_title": "<...>", ...}
{"timestamp": 1700000000.456, "type": "mouse.singleclick", "x": 100, "y": 200, "button": "left", "children": [...]}
{"timestamp": 1700000000.789, "type": "key.type", "text": "hello", "children": [...]}
...
```

- **Line 1** is always the `_meta` dict (plain `json.dumps`).
- **Subsequent lines** are Pydantic events serialized via `.model_dump_json()`.
- Events are time-ordered.
- `mouse.move` events are excluded by default (both paths). `--include-moves` keeps them.

## Privacy-aware window switches (chunk path only)

`build_privacy_filter(privacy_mode, cloud_intent, capture_dir)` in `exporter.py` returns a closure that:

1. Checks `.menubar_overrides.json` for runtime user toggles. `"exclude"` → return `None` (suppressed). `"allow"` → unchanged.
2. Otherwise runs `DefaultContextClassifier.classify` + `DefaultPolicyEvaluator.evaluate`.
3. Applies the resulting `PrivacyAction`:
   - **EXCLUDE** → return `None`. Event suppressed entirely.
   - **MASK_WINDOW** → return `event.model_copy(update={"window_title": event.app_name, "domain": None})`. Title becomes app name; domain nulled.
   - **TEXT_REDACT, ALLOW, others** → return event unchanged. Scrubbing of `key.type` text and similar is deferred to the scrub pipeline.
4. If `cloud_intent=True`, mode is forced to `PUBLIC`.

This filter is currently invoked **only by the chunk processor** when building per-chunk JSONL during cloud-intent recordings. The factory exists in `exporter.py` (and the kwarg slot exists in `_write_events`), but the full-recording CLI export path does not call it.

## Window switch deduplication

`deduplicate_window_events()` collapses raw window event rows on `(app_bundle_id, window_id)`. Title-only changes within the same window do NOT trigger a new switch event. A late-arriving `browser_url` (common with async AX URL extraction) patches the `domain` of the already-emitted event via `model_copy()`.

## Load-bearing invariants

- **First line is `_meta` for chunk-processor output.** Recovery output (`_recover_chunk_metadata`) skips the header and writes raw DB rows. Parsers should check `data.get("_meta")` to identify the header — and tolerate its absence on recovery files.
- **`format_version: 2` is current.** v1 manifests existed historically; v2 is the live format. Bumping requires updating Cloud Run's manifest routing too.
- **Atomic write everywhere.** Both paths use `.tmp` + `os.rename`. Crash mid-write leaves no partial JSONL, only the prior version (or nothing).
- **Privacy filter applies only to the chunk path.** CLI `screencap export` does not run a privacy filter. Cloud-safety on full export must come from capture-time enforcement (already-blocked content was never written) and post-hoc scrubbing.
- **Dedup on `(app_bundle_id, window_id)`, not title.** Title-only changes are noise.
- **Initial window context query timestamp rewrite is required.** `start_ts - 0.001` ensures the synthetic event sorts first. Removing the rewrite breaks the first-action window-context guarantee.
- **Cloud intent forces PUBLIC privacy mode.** Local recordings respect the config-file mode. Don't assume the configured mode applies — check `cloud_intent`.
- **MASK_WINDOW null both `window_title` AND `domain`.** Replacing only the title would still leak via `domain` for browsers.
- **Mouse.move excluded by default.** Including moves bloats the JSONL by 100×+. Only re-enable for specific tools (debugging, UI replay).
- **Chunk processor uses raw sqlite3, not SQLAlchemy.** Loads faster, avoids ORM overhead, and `query_only=ON` is read-safe during writes.
- **`build_privacy_filter` reads `.menubar_overrides.json` at filter construction.** Subsequent menubar toggles do not affect an in-flight export.
- **Filter `None` return = suppress.** Don't return the unmodified event by mistake — it would defeat EXCLUDE.

## Before you change it

- Bumping `format_version`: every consumer (viewer, scrubber, Cloud Run) must handle both versions or be updated in lockstep.
- Adding a field to the `_meta` header: `build_export_metadata` is the single place to change. Ensure consumers handle missing fields (older recordings will lack new keys).
- Changing the privacy filter for window switches: it ALSO runs at scrub time on `WindowSwitchEvent` text. Don't add side effects (e.g., logging) — runs on every event.
- Adding a new event type: the chunk processor's raw-sqlite3 query reads `action_event.*` flat — make sure the new type's columns are already in the schema. Otherwise migrate first.
- Changing the chunk time-range query: `[start_ts, end_ts)` is half-open; events at exactly `end_ts` belong to the next chunk. Test boundary handling.
- Removing `mouse.move` from raw capture entirely: would simplify export but break drag detection (stage 9 needs the moves to compute distance).

## See also

- [event-system.md](./event-system.md) — the 11-stage pipeline applied here
- [database.md](./database.md) — raw rows + schema
- [privacy.md](./privacy.md) — what the privacy filter checks
- [scrubbing.md](./scrubbing.md) — post-export text scrubbing
- [segmentation.md](./segmentation.md) — what the manifest carries alongside JSONL
