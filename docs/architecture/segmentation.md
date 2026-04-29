# Task Segmentation

## What it does

Splits a recording into discrete tasks (e.g., "writing tests for module X", "responding to email"). Two segmentation paths coexist; the recording chooses one at start time.

## The two paths

```
┌─────────────────────────────────────────────────────────┐
│   recording start                                       │
│   --segmentation-mode CLI flag OR config.toml           │
│      │                          │                       │
│      ▼                          ▼                       │
│   "idle" (legacy v1)       "llm" (default v2)           │
└──────┬──────────────────────────┬───────────────────────┘
       │                          │
       ▼                          ▼
─── Path 1: idle-gap ───      ─── Path 2: LLM ───

client-side segmentation     client writes minimal manifest
in chunk_processor           (stats + chunk boundaries only)
       │                          │
       ▼                          ▼
chunk_NNNN_manifest.json     chunk_NNNN_manifest.json
{                            {
  "chunk_index": 0,            "format_version": 2,
  "chunk_start": ...,          "chunk_index": 0,
  "tasks": [                   "stats": {total_events, ...},
    {derived_name,             "blocked_intervals": [...]
     dominant_app, ...},     }
    ...
  ],
  "summary": {...}
}                            ← legacy has NO format_version key
       │                          │
       ▼                          ▼
─── upload to GCS ──────────────────────────
       │                          │
       ▼                          ▼
─── Cloud Run process-recording ────────────
       │                          │
       ▼                          ▼
_merge_tasks() — glues          _process_v2_manifests() —
cross-chunk tasks               LLM segmentation via Gemini
       │                          │
       ▼                          ▼
sessions/<name>/timeline.json with segmentation_method
```

## Path 1: idle-gap (legacy, no format_version)

`task_manifest._generate_manifest_legacy` runs at chunk rotation when `segmentation_mode == "idle"`. It:

1. Reads action events (excluding `move`) and window events for the chunk's time range.
2. `_segment_tasks(events, rest_threshold=120s)`: walks events sorted by timestamp; splits into a new task whenever the inter-event gap exceeds `rest_threshold`.
3. For each task computes `dominant_app` via `_compute_dominant_app`: builds wall-clock dwell time per bundle ID using the window event timeline, returns the longest.
4. Derives a name via `_derive_task_name`: applies `_TITLE_PARSERS` regex per known app (VS Code, Chrome, Safari, Firefox, Slack, Terminal, iTerm2, Finder), or falls back to slugified `app_name + title`.
5. Writes manifest with full `tasks` array. **No `format_version` key is written** — this is how Cloud Run distinguishes legacy from v2.

Cloud Run's `_merge_tasks` then glues cross-chunk tasks: if the last task of chunk N has `rest_after_s == 0`, the chunk gap ≤ 5s, and the inter-task idle is below threshold, it merges with the first task of chunk N+1. Re-computes the dominant app.

Legacy manifest shape:

```json
{
  "chunk_index": 0,
  "chunk_start": 1700000000.0,
  "chunk_end": 1700000060.0,
  "rest_threshold_secs": 120,
  "tasks": [
    {"derived_name": "vscode-auth", "dominant_app": "...", "start_ts": ..., "end_ts": ..., ...}
  ],
  "summary": {"total_tasks": 1, "total_active_s": 60.0, ...},
  "blocked_intervals": [...]
}
```

## Path 2: LLM (v2, default)

Client side is minimal. `task_manifest._generate_manifest_v2` writes:

```json
{
  "format_version": 2,
  "chunk_index": 0,
  "chunk_start": 1700000000.0,
  "chunk_end": 1700000060.0,
  "stats": {
    "total_events": 1234,
    "total_window_switches": 56
  },
  "blocked_intervals": [...]
}
```

No `tasks` array. Cloud Run does the work after upload.

### Cloud Run pipeline (`scripts/process-recording/main.py`)

`_process_v2_manifests` orchestrates:

1. **`_derive_activity_summary`** streams `events_NNNN.jsonl` files into a compact timeline. Per window-switch entry: relative timestamp (H:MM:SS), duration, app, title, category, up to 5 typed snippets, up to 10 shortcuts, click and scroll counts. Caps at 200 entries. Pulls up to 20 transcript segments per chunk. Returns `{summary, entries, time_map, session_start, session_end, raw_timestamps, raw_window_events}`.

2. **`_llm_segment_session`** → **`_call_gemini`**: `gemini-2.5-flash` with `response_mime_type="application/json"` and `_RESPONSE_SCHEMA` (structured output). Temperature 0.1. Per-task schema requires `start_time`, `end_time`, `name`, `description`, `category` (6-enum), `apps_used`, `confidence` (3-enum). Top-level requires `tasks`, `summary`, `tags`.

3. **`_validate_llm_tasks`**:
   - Convert relative→Unix via `time_map` exact lookup, fallback to `session_start + _parse_relative_time(rel)`.
   - Clamp to session bounds.
   - Reject zero-duration tasks.
   - Reject overlapping tasks (1s tolerance).
   - Validate tags: regex `^[a-z0-9][a-z0-9-]{0,30}$`, dedup, max 8.

4. **`_map_tasks_to_chunks`**: intersects each task's `[start_ts, end_ts)` with each chunk's `[chunk_start, chunk_end)` to produce `source_chunks` with `start_offset_s`, `end_offset_s`. ffmpeg uses these to stitch chunked media into per-task files.

5. Sets `segmentation_method = "llm"`.

### Fallback

If any step fails (no activity, LLM returns None, validation fails), Cloud Run falls back:

- **`_simple_segment_from_events`** runs idle-gap segmentation using cached timestamps + window events from the activity summary call (no GCS re-read). Same `rest_threshold=120s`.
- **`_stats_summary`** computes `time_breakdown` from activity categories with ≥5% share, builds `overview` from dominant category + top apps. Returns `{overview, primary_focus, time_breakdown, key_accomplishments: []}`.
- Sets `segmentation_method = "idle"`.

## Output: `timeline.json`

Written to `sessions/<name>/timeline.json` after Cloud Run processing.

```json
{
  "recording_name": "...",
  "display_name": "Refactor the auth middleware",
  "show_on_website": true,
  "segmentation_method": "llm",
  "processed_at": "2026-...",
  "processor_version": "2.1.0",
  "total_tasks": 5,
  "total_chunks": 12,
  "total_duration_s": 7200.5,
  "total_active_s": 6900.0,
  "tags": ["coding", "auth"],
  "tasks": [
    {
      "folder": "000_refactor-auth-middleware",
      "start_ts": 1700000000.0,
      "end_ts": 1700001500.0,
      "duration_s": 1500.0, "duration_human": "25m 0s",
      "event_count": 234,
      "dominant_app": "com.microsoft.VSCode",
      "dominant_app_name": "Visual Studio Code",
      "dominant_title": "auth.py — myproject",
      "derived_name": "vscode-auth-py",
      "source_chunks": [...],
      "merged_across_chunks": true,
      "has_transcript": false,
      "has_video": true,
      "video_size_mb": 45.2,
      "rest_after_s": 60.0,
      "all_apps": {...},
      "index": 0,
      "name": "Refactor the auth middleware",      ← LLM-only
      "description": "Removed legacy session...",  ← LLM-only
      "category": "development",                   ← LLM-only
      "apps_used": ["VS Code"],                    ← LLM-only
      "confidence": "high"                         ← LLM-only
    }, ...
  ],
  "summary": {                                     ← LLM-only or fallback
    "overview": "...",
    "primary_focus": "Code refactoring",
    "time_breakdown": {"development": 0.85, "communication": 0.15},
    "key_accomplishments": [...]
  }
}
```

`display_name` derivation order: LLM `key_accomplishments[0]` → first task `name` → dominant app + category → formatted recording timestamp.

## Configuration

`get_segmentation_mode()` in `screencap/config.py` reads `segmentation_mode` from `~/.screencap/config.toml`. Default `"llm"`. Valid: `"idle"`, `"llm"`.

CLI flag: `screencap start --segmentation-mode [idle|llm]`. The flag takes priority over the config file value. Invalid values exit with a helpful error.

The mode is captured at recording start and persisted **via the manifest format** (the manifest type itself encodes the choice — `format_version: 2` for LLM, no `format_version` for legacy). Cloud Run reads `manifests[0].get("format_version", 0)` and routes: `>= 2` → LLM path, `< 2` (including missing key, which legacy never sets) → idle-gap merge path. Independent of any current config setting.

> **Recovery exception.** The mode is **not** stored in `.recording_intent`. If the original chunk processor failed and `screencap upload` runs `_recover_chunk_metadata` to backfill manifests, that recovery path calls `get_segmentation_mode()` against current config — not the mode in effect at recording time. If the user changed `segmentation_mode` in `config.toml` between recording and upload, recovered manifests will use the new mode. Either persist `segmentation_mode` in `.recording_intent`, or treat the upload-time config as authoritative for recovered chunks; today the code does the latter.

## Load-bearing invariants

- **Manifest `format_version` is the source of truth for Cloud Run.** Reading `config.toml` server-side would be wrong — the recording was created with a specific mode and that decision is baked into the manifest. Legacy manifests omit the key entirely; Cloud Run reads with `.get("format_version", 0)` so missing → 0 → idle-gap path. Don't add `format_version: 1` to legacy manifests; the test suite asserts its absence.
- **Segmentation mode is NOT persisted in `.recording_intent`.** Recovery (`_recover_chunk_metadata`) reads current `get_segmentation_mode()` to fill in missing manifests. Recordings whose original chunk processor failed will be recovered with the live config's mode, not the mode that was in force when recording started.
- **CLI flag wins over config.toml.** `--segmentation-mode` is captured at recording start.
- **`_LLM_ENRICHED_FIELDS`** (`name`, `description`, `category`, `apps_used`, `confidence`) are conditional. Idle-segmented tasks lack them. Frontend code must handle missing keys.
- **Tag regex `^[a-z0-9][a-z0-9-]{0,30}$`, max 8 tags.** Validation strips invalid tags, dedups, caps. Don't bypass — Cloud Run's tag system depends on these constraints.
- **Time validation: exact lookup OR fallback parse.** `time_map` records every relative timestamp emitted in the activity summary. LLM may produce timestamps that aren't exact strings — fallback parses to Unix.
- **No overlap, 1s tolerance.** Adjacent tasks within 1s are accepted (rounding); larger overlaps fail validation.
- **`_simple_segment_from_events` reuses cached data.** Doesn't re-read GCS. The `_derive_activity_summary` call returns `raw_timestamps` and `raw_window_events` for this purpose.
- **`MAX_ACTIVITY_ENTRIES = 200`.** Caps the LLM context size. Longer recordings collapse less-active periods first.
- **`PROCESSOR_VERSION` goes into `timeline.json`.** Bump when output schema changes. Frontend can branch on it.
- **Gemini schema is closed.** `response_mime_type="application/json"` + `_RESPONSE_SCHEMA` enforces structured output. Adding new fields requires schema update + cloud function redeploy.
- **Idle-gap fallback signals via `segmentation_method = "idle"`.** Frontend can degrade gracefully (hide LLM-only fields).

## Before you change it

- Adding a new task field: decide LLM-only vs always-present. Always-present fields go through `_process_task` for both paths. LLM-only goes in `_LLM_ENRICHED_FIELDS`.
- Bumping the LLM schema: update `_RESPONSE_SCHEMA`, deploy the cloud run service, validate against historical manifests with `_validate_llm_tasks`.
- Changing the rest threshold: lives in `screencap/config.py:get_rest_threshold` (env: `SCREENCAP_REST_THRESHOLD`, default 120s). Both client v1 and Cloud Run fallback use it.
- Adding a new manifest format_version (v3): update both client `task_manifest.py` dispatch and Cloud Run routing in `_process_*_manifests`. Don't break v1/v2 reads.
- Removing v1: still in use by older recordings; Cloud Run must continue to handle them. Plan a grace window.
- Changing `_LLM_PROMPT`: re-run benchmarks. The prompt is calibrated against the activity summary format.

## See also

- [export-pipeline.md](./export-pipeline.md) — what `events_NNNN.jsonl` looks like
- [recording-engine.md](./recording-engine.md) — chunk rotation triggers segmentation
- [database.md](./database.md) — `recording.db` is the source for the activity summary
- `decisions/` — why LLM is default, fallback strategy, processor version bumps
- `research/` — Gemini prompt iterations, segmentation accuracy benchmarks
