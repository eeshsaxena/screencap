# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

ScreenCap is a macOS CLI for screen recording. It wraps the vendored screencap-engine package (`sc_engine` module). Python >= 3.10, macOS only.

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

No linting is configured for the root `screencap` package. The vendored sub-packages use `ruff` (run inside their directories with `uv run ruff check .`).

## Architecture

**Entry point:** `screencap.cli:cli` — a Click command group registered as `screencap` console script.

**CLI commands:** `start`, `list`, `view`, `info`, `export`, `upload`, `download`, `transcribe`, `stop`, `update` — defined in `src/screencap/cli.py`.

**Core modules (all in `src/screencap/`):**
- `config.py` — reads `~/.screencap/config.toml` with env var overrides (`SCREENCAP_RECORDINGS_DIR`, `SCREENCAP_AUDIO_DEFAULT`). Priority: env vars > config.toml > defaults. Uses module-level `_config_cache` dict (reset to `None` in tests).
- `recorder.py` — wraps `sc_engine.Recorder` context manager. Custom SIGINT handler: first Ctrl+C = graceful stop, second = force quit.
- `catalog.py` — scans recordings dir, reads metadata from SQLite. Supports two DB schemas: `recording.db` (tables: `recording`, `action_event`) and `capture.db` (tables: `capture`, `events`). Returns `RecordingInfo` NamedTuples.
- `viewer.py` — opens `viewer.html` via macOS `open` command.

**Vendored packages (under `packages/`):**
- `screencap-engine` (`sc_engine`) — multi-process recording (pynput, mss, av/ffmpeg, sounddevice). SQLAlchemy + Alembic for per-capture SQLite DBs. Has its own entry point (`capture`).

The vendored package is co-installed via the root `pyproject.toml` `packages.find.where` — it is NOT a separate pip install.

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

**Unified processing pipeline:** Both CLI `screencap export` and the chunk processor share the same event processing path:
1. Raw DB rows → `dict_to_action_event()` (`sc_engine/convert.py`) → Pydantic events
2. `process_events()` (`sc_engine/processing.py`) — 11-stage merge/detect pipeline
3. `deduplicate_window_events()` + `interleave_window_events()` (`sc_engine/processing.py`)
4. Privacy filtering (screencap layer) → JSONL serialization via `model_dump_json()`

**Two export paths:**
- **CLI export** (`src/screencap/exporter.py`) — `CaptureSession.export_events()` → `_write_events()`. Full recording export with optional privacy filter.
- **Chunk processor** (`src/screencap/chunk_processor.py:_export_events()`) — per-chunk time-range export using raw `sqlite3` queries → shared pipeline. Includes initial window context (last window event before chunk start).

**`events.jsonl` format (v2):**
- Line 1: `_meta` header with `format_version: 2`, `screencap_version`, `exported_at`
- Remaining lines: Pydantic event JSON — processed action events (`mouse.singleclick`, `key.type`, etc.) interleaved with `window.switch` events
- `window.switch` events are deduplicated by `(app_bundle_id, window_id)` — title-only changes are ignored
- `mouse.move` events excluded by default in both paths

**Privacy-aware `window.switch` events:** EXCLUDE apps → suppressed entirely, MASK_WINDOW → title replaced with app name, OCR_FALLBACK → suppressed for cloud-intent uploads. Privacy filtering happens in the screencap layer (`exporter.py` / `chunk_processor.py`), not in `sc_engine`.

**Scrubbing pipeline:** `_scrub_events_jsonl()` scrubs `key.type` and `key.shortcut` text + children `key_char`, and `window.switch` titles.

## Key Patterns

- All user-facing output uses `rich.console.Console` (no bare `print()`).
- Heavy imports are deferred inside CLI command bodies to keep `screencap --help` fast.
- All source files use `from __future__ import annotations`.
- SQLite access in the `screencap` layer uses raw `sqlite3`, not SQLAlchemy.
- Dual DB schema support: `catalog.py` detects which schema via `sqlite_master` queries.
- Recording dirs live at `~/.screencap/recordings/<name>/`.
- Tests use `click.testing.CliRunner`, `unittest.mock.patch`, and `tmp_path` fixtures with inline SQLite setup.
