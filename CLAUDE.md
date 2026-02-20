# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

ScreenCap is a macOS CLI for screen recording with built-in PII scrubbing. It wraps two vendored OpenAdapt packages for capture and privacy redaction. Python >= 3.10, macOS only.

## Common Commands

```bash
# Install (editable dev mode)
pip install -e ".[dev]"

# Install with privacy/scrubbing deps (~500 MB)
pip install -e ".[dev,privacy]"

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

**Four CLI commands:** `start`, `list`, `view`, `scrub` — defined in `src/screencap/cli.py`.

**Core modules (all in `src/screencap/`):**
- `config.py` — reads `~/.screencap/config.toml` with env var overrides (`SCREENCAP_RECORDINGS_DIR`, `SCREENCAP_AUDIO_DEFAULT`). Priority: env vars > config.toml > defaults. Uses module-level `_config_cache` dict (reset to `None` in tests).
- `recorder.py` — wraps `openadapt_capture.Recorder` context manager. Custom SIGINT handler: first Ctrl+C = graceful stop, second = force quit.
- `catalog.py` — scans recordings dir, reads metadata from SQLite. Supports two DB schemas: `recording.db` (tables: `recording`, `action_event`) and `capture.db` (tables: `capture`, `events`). Returns `RecordingInfo` NamedTuples.
- `scrubber.py` — copies recording dir to `<name>-scrubbed/`, then scrubs screenshots, DB text fields, and `transcript.json`. Never mutates originals. Video scrubbing is unsupported.
- `viewer.py` — opens `viewer.html` via macOS `open` command.

**Vendored packages (under `packages/`):**
- `openadapt-capture` — multi-process recording (pynput, mss, av/ffmpeg, sounddevice). SQLAlchemy + Alembic for per-capture SQLite DBs. Has its own entry point (`capture`).
- `openadapt-privacy` — Presidio + spaCy NER for text/image PII scrubbing. Factory pattern via `ScrubProvider.get_scrubber()`.

Both vendored packages are co-installed via the root `pyproject.toml` `packages.find.where` — they are NOT separate pip installs.

## Key Patterns

- All user-facing output uses `rich.console.Console` (no bare `print()`).
- Heavy imports are deferred inside CLI command bodies to keep `screencap --help` fast.
- All source files use `from __future__ import annotations`.
- SQLite access in the `screencap` layer uses raw `sqlite3`, not SQLAlchemy.
- Dual DB schema support: both `catalog.py` and `scrubber.py` detect which schema via `sqlite_master` queries.
- Recording dirs live at `~/.screencap/recordings/<name>/`; scrubbed copies at `<name>-scrubbed/` (excluded from list output).
- Tests use `click.testing.CliRunner`, `unittest.mock.patch`, and `tmp_path` fixtures with inline SQLite setup.
