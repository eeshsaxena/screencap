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

## Key Patterns

- All user-facing output uses `rich.console.Console` (no bare `print()`).
- Heavy imports are deferred inside CLI command bodies to keep `screencap --help` fast.
- All source files use `from __future__ import annotations`.
- SQLite access in the `screencap` layer uses raw `sqlite3`, not SQLAlchemy.
- Dual DB schema support: `catalog.py` detects which schema via `sqlite_master` queries.
- Recording dirs live at `~/.screencap/recordings/<name>/`.
- Tests use `click.testing.CliRunner`, `unittest.mock.patch`, and `tmp_path` fixtures with inline SQLite setup.
