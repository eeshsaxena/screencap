# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

ScreenCap is a macOS CLI for screen recording. The recording engine lives at `src/screencap/engine/` as an internal sub-package. Python >= 3.10, macOS only.

## Common Commands

```bash
# Install (editable dev mode)
pip install -e ".[dev]"

# Run all tests
pytest tests/

# Run a single test file or test
pytest tests/test_cli.py
pytest tests/test_catalog.py::test_list_recordings_empty

# Run with coverage
pytest tests/ -v --cov
```

Linting uses `ruff` for the engine sub-package: `ruff check src/screencap/engine/`.

## Architecture

**Entry point:** `screencap.cli:cli` — a Click command group registered as `screencap` console script.

**CLI commands:** `start`, `list`, `view`, `info`, `export`, `upload`, `download`, `transcribe`, `stop`, `update` — defined in `src/screencap/cli.py`.

**Core modules (all in `src/screencap/`):**
- `config.py` — reads `~/.screencap/config.toml` with env var overrides (`SCREENCAP_RECORDINGS_DIR`, `SCREENCAP_AUDIO_DEFAULT`). Priority: env vars > config.toml > defaults. Uses module-level `_config_cache` dict (reset to `None` in tests).
- `recorder.py` — wraps `screencap.engine.Recorder` context manager. Signal handlers installed BEFORE `Recorder.__enter__()` to cover the entire setup window. SIGINT: first = graceful stop, second = force quit (kills children + `os._exit`), third = immediate `os._exit`. SIGTERM: graceful stop (used by `screencap stop`). Handlers guard against `recorder=None` (signal during setup) and fall back to `multiprocessing.active_children()` when `_child_pids` is empty. The recommended way to stop a recording programmatically is `screencap stop` (sends SIGTERM, 30s timeout, fallback to force-kill). Ctrl+C is a convenience shortcut for foreground terminal use only.
- `catalog.py` — scans recordings dir, reads metadata from SQLite. Reads `recording.db` (tables: `recording`, `action_event`). Returns `RecordingInfo` NamedTuples.
- `viewer.py` — opens `viewer.html` via macOS `open` command.

**Engine sub-package (`src/screencap/engine/`):**
- Multi-process recording engine (pynput, mss, av/ffmpeg, sounddevice). SQLAlchemy for per-capture SQLite DBs.
- `engine/recorder.py` — multi-process recorder (reader threads → event_q → writer processes)
- `engine/capture.py` — `CaptureSession` class for loading and iterating events/actions
- `engine/events.py` — Pydantic event models (MouseMoveEvent, KeyDownEvent, etc.)
- `engine/processing.py` — 11-stage event merging pipeline (clicks, drags, typing)
- `engine/db/` — SQLAlchemy database layer (Engine, session factory, Base, models, CRUD)
- `engine/window/` — platform-specific active window capture
- `engine/config.py` — recording config (pydantic-settings, RECORD_VIDEO, RECORD_AUDIO, etc.)
- `engine/video.py` — video encoding (av/ffmpeg)
- `engine/audio.py` — audio recording + transcription
- `engine/dedup.py` — perceptual hashing (dHash) for screenshot deduplication
- `engine/visualize/` — demo GIF and HTML viewer generation

All engine imports from `screencap` source are deferred (inside function bodies) to keep `screencap --help` fast. `engine/__init__.py` exposes a curated 4-name surface (`Capture`, `CaptureSession`, `create_html`, `__version__`); deeper symbols import directly from their submodules.

## Privacy System (`src/screencap/privacy/`)

Two-layer privacy enforcement: capture-time filtering + post-recording scrubbing.

**Core modules:**
- `actions.py` — `PrivacyAction` enum (EXCLUDE → MASK_WINDOW → MASK_REGION → TEXT_REDACT → OCR_FALLBACK → ALLOW), `ActionDecision` dataclass, `stricter()` comparator.
- `policy.py` — `PrivacyMode` enum (public/internal), `ContextClass` enum (10 app categories), `_ACTION_MATRIX` mapping every (context, mode) pair to an action, `PrivacyConfig` (parsed from `[privacy]` in config.toml), `DefaultPolicyEvaluator` with 5-level precedence (exclude_apps > allow_apps > mask_domains > mask_title_patterns > matrix).
- `context.py` — `DefaultContextClassifier` that maps bundle IDs, browser domains, and window titles to `ContextClass`. Includes known bundle ID map (~80 apps), browser domain map, and title heuristics. `associate_screenshot()` correlates screenshot timestamps to window events using bisect-based nearest-event lookup.
- `recorder_enforcement.py` — `RecorderPrivacyFilter` for capture-time gating. Observes window events, evaluates policy, blocks screenshots and nulls keystrokes. Multiple independent blocking sources (app_policy, secure_input, secure_field) with per-source hold timers. Starts fail-closed.
- `masking.py` — Pillow-based screenshot masking (full-window blur) for MASK_WINDOW actions.
- `reasons.py` — `ReasonCode` constants and `AuditEntry` for traceability.

**Key design decisions:**
- Fail-closed by default: filter blocks capture until first window event; errors trigger fail-closed until next successful event.
- `SCREENCAP_PRIVACY_MODE` env var can only tighten, never loosen the mode vs config.toml.
- `allow_apps` cannot bypass matrix EXCLUDE (e.g., password managers are always excluded regardless of allow list).
- `PrivacyConfig.app_classes` is frozen (`MappingProxyType`) after construction.
- Transition hold (1.0s) after switching from a blocked app covers macOS Cmd+Tab animation (200-350ms).
- `shared` mode is defined in the matrix but raises `InvalidPrivacyConfigError` until MASK_REGION is implemented.
- The `setup` CLI command runs an interactive TUI (curses) to classify installed apps, writing results to `[privacy]` in config.toml. First `screencap start` prompts for setup if no `[privacy]` section exists.

## Export System

**Unified processing pipeline:** All three export callers (CLI export, chunk processor, recovery) route through `unified_export_events` in `src/screencap/engine/export.py` — the single source of truth for the row-to-Pydantic-event transform:
1. Raw DB rows → `dict_to_action_event()` (`screencap/engine/convert.py`) → Pydantic events
2. `process_events()` (`screencap/engine/processing.py`) — 11-stage merge/detect pipeline
3. `deduplicate_window_events()` + optional `window_filter` + `interleave_window_events()` (`screencap/engine/processing.py`)
4. Returns `Iterator[BaseEvent]` — no DB, no file I/O, no privacy semantics for mouse coordinates. Callers handle row fetching, `disabled` filter, `mouse.move` opt-out, `_meta` header, and atomic writes.

**Three callers, one callable:**
- **CLI export** (`src/screencap/exporter.py`) — `screencap export <recording>` → `Capture.load(...).export_events(include_moves)` (which delegates to `unified_export_events` with `window_filter=None`, materialized to a `list[BaseEvent]` to preserve the public ORM contract) → `_write_events()` for the file/stdout write. Atomic `.tmp` + `os.rename` for files; stdout for `output_path is None`. No privacy filter.
- **Chunk processor** (`src/screencap/chunk_processor.py:_export_events()`) — per-chunk time-range export using raw `sqlite3` queries (with `(disabled IS NULL OR NOT disabled)` filter) → `unified_export_events` (with `window_filter=build_cloud_window_filter(self._cloud_intent, ...)`) → `write_events_jsonl` (shared streaming atomic writer). Includes initial window context (last window event before chunk start, timestamp rewritten to `start_ts - 0.001`).
- **Recovery** (`src/screencap/cli.py:_recover_chunk_metadata`) — invoked by `screencap upload` when chunk videos exist but `events_NNNN.jsonl` files are missing. Same fetch + `unified_export_events` + `write_events_jsonl` path as the chunk processor. **`cloud_bound: bool` is a REQUIRED keyword** (no default — omitting it raises `TypeError`); `screencap upload` always passes `cloud_bound=True` regardless of `.recording_intent`. Recovery now produces v2 format byte-identical to the chunk processor for the same time range (modulo the `_meta` `exported_at`).

**`events.jsonl` format (v2):**
- Line 1: `_meta` header with `format_version: 2`, `screencap_version`, `exported_at`
- Remaining lines: Pydantic event JSON — processed action events (`mouse.singleclick`, `key.type`, etc.) interleaved with `window.switch` events
- `window.switch` events are deduplicated by `(app_bundle_id, window_id)` — title-only changes are ignored
- `mouse.move` events kept by default on all three paths; CLI exposes `--exclude-moves` for opt-out. Cloud Run filters moves at iteration time; the scrub layer drops in-interval moves for sensitive contexts.

**Privacy-aware `window.switch` events:** EXCLUDE apps → suppressed entirely, MASK_WINDOW → title replaced with app name (and `domain` nulled), TEXT_REDACT → passes through with post-capture scrubbing. Cloud-bound callers (chunk processor and recovery) MUST construct the filter via `build_cloud_window_filter(cloud_bound, privacy_mode, capture_dir)` in `src/screencap/privacy/filter.py` — the factory returns `None` when `cloud_bound=False` and a cloud-mode filter when `True`. Wiring it unconditionally (`window_filter=build_cloud_window_filter(...)`) eliminates the `if cloud_intent: build_filter() else None` pattern that previously caused the Slack-title leak. The CI guard at `tests/test_privacy_filter_call_graph.py` is an AST walker that scans `src/screencap/` and fails the build if any caller imports `build_privacy_filter` directly outside sanctioned sites or passes `window_filter=None` literally to `unified_export_events` from cloud-bound code.

**Recovery's privacy posture is driven by call-site context** (the `cloud_bound` parameter), not by `.recording_intent`. The reasoning: `screencap upload` knows the data is becoming cloud-bound at upload time regardless of what the intent file says, which closes the local-then-uploaded threat case (recording captured as `destination=local` and later uploaded would otherwise leak titles). `.recording_intent` is documented as the fallback for hypothetical future non-upload recovery contexts (none exist today).

**Scrubbing pipeline:** `_scrub_events_jsonl()` scrubs `key.type` and `key.shortcut` text + children `key_char`, and `window.switch` titles. **It also drops `mouse.move` events whose timestamp falls inside an interval whose privacy action is in `SCRUB_BLOCK_ACTIONS = {EXCLUDE, MASK_WINDOW, TEXT_REDACT, OCR_FALLBACK}`** (defined in `src/screencap/privacy/actions.py`). This is broader than `BLOCK_ACTIONS` — TEXT_REDACT covers code editors / admin consoles, OCR_FALLBACK covers unverified browsers — because pointer geometry inside content-sensitive contexts is comparably sensitive (which terminal line was being edited, which credentials field was being hovered). Drag children with inline `mouse.move` entries are dropped via the same predicate. **Load-bearing ordering:** `_recover_chunk_metadata` MUST be followed by `scrub_recording` for cloud-bound recordings before upload — the scrub-layer pointer suppression only protects recovered cloud-bound JSONL when this ordering holds.

**Detection pipeline (`src/screencap/privacy/`):**
- `__init__.py` — `DetectionPipeline` composes detectors → resolver → filters. `Detection` dataclass, `EntityType` constants, `TextDetector`/`DetectionFilter` protocols, `Anonymizer`, `create_default_pipeline()` factory.
- `pii.py` — `PiiDetector` wraps Presidio with GLiNER NER backend (default) or spaCy fallback. Person threshold + allowlist filtering.
- `regex.py` — `RegexDetector` for emails, URLs, credit cards, SSNs, phone numbers.
- `secrets.py` — `DetectSecretsDetector` wraps `detect-secrets` for API keys, private keys, JWTs, connection strings.
- `resolver.py` — `DetectionResolver` resolves overlapping spans: same-source overlaps union, cross-source overlaps kept separate, compatible nesting preserved (EMAIL/PASSWORD/API_KEY inside CONNECTION_STRING). Source priority: secrets=40, regex=30, pii-gliner=20, pii-presidio=10.
- `filters.py` — `HeuristicFilter` rejects common FPs: short PERSON, month/UI-keyword as PERSON, box-drawing as ADDRESS, malformed SSN, separator-less PHONE.
- `entity_mapping.py` — Centralized label mapping for Presidio + GLiNER NER backends.

**Benchmarking:** `benchmarks/benchmark_pii.py` runs the detection pipeline against a 40-case corpus (`tests/privacy/fixtures/test_corpus.py`) and outputs precision/recall/F1 metrics. Use `--engine full` for the complete pipeline or `--compare` to diff two result files.

## Task Segmentation

**Two segmentation modes** controlled by `segmentation_mode` config (`config.toml` or `--segmentation-mode` CLI flag, default `"llm"`):
- **`llm` (default):** Client generates simplified v2 manifests (`format_version: 2`) with chunk metadata only (stats, timestamps). Cloud Run derives activity summary from events JSONL + transcripts, calls Gemini Flash for intelligent task boundaries with names, descriptions, categories. Falls back to idle-gap segmentation if LLM fails.
- **`idle` (legacy):** Client generates v1 manifests with tasks array from idle-gap detection (120s threshold). Cloud Run merges cross-chunk tasks via `_merge_tasks()`.

**Cloud Run LLM pipeline** (`scripts/process-recording/main.py`):
1. `_derive_activity_summary()` — stream-parse events JSONL into compact activity timeline
2. `_llm_segment_session()` → `_call_llm()` → `_call_gemini()` — Gemini Flash with structured output
3. `_validate_llm_tasks()` — validate, convert relative→Unix timestamps, check overlaps
4. `_map_tasks_to_chunks()` — map task boundaries to source chunk indices for media stitching
5. Fallback: `_simple_segment_from_events()` + `_stats_summary()` — idle-gap with stats-based summary

**Config:** `get_segmentation_mode()` in `config.py` reads from `config.toml`. CLI flag `--segmentation-mode` on `screencap start` takes priority. The manifest format itself controls Cloud Run behavior: v2 manifests always attempt LLM, v1 manifests use legacy merge.

**Enriched output:** `timeline.json` includes `segmentation_method`, per-task `name`/`description`/`category`/`apps_used`/`confidence`, and session-level `summary` with `overview`/`primary_focus`/`time_breakdown`/`key_accomplishments`.

## Key Patterns

- All user-facing output uses `rich.console.Console` (no bare `print()`).
- Heavy imports are deferred inside CLI command bodies to keep `screencap --help` fast.
- All source files use `from __future__ import annotations`.
- SQLite access in the `screencap` layer uses raw `sqlite3`, not SQLAlchemy.
- Recording dirs live at `~/.screencap/recordings/<name>/`.
- Tests use `click.testing.CliRunner`, `unittest.mock.patch`, and `tmp_path` fixtures with inline SQLite setup.
- `engine/__init__.py` exposes only `Capture`, `CaptureSession`, `create_html`, and `__version__`; all other engine symbols import from their defining submodules.

## Documented Solutions

`docs/solutions/` — documented solutions to past problems (bugs, best practices, workflow patterns), organized by category with YAML frontmatter (`module`, `tags`, `problem_type`). Relevant when implementing or debugging in documented areas.
