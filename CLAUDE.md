# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

ScreenCap is a macOS CLI for screen recording. The recording engine lives at `src/screencap/engine/` as an internal sub-package. Python >= 3.10, macOS only.

A native SwiftUI app shell lives at `macos/` and wraps the bundled CLI; see "macOS SwiftUI app shell" below for details.

### Daemon architecture (Phase 2)

The recording engine is supervised by a background daemon. CLI live-state commands (`screencap start` / `stop` / `status`) are thin HTTP clients of the daemon's `/v0/*` API over a UNIX socket at `~/.screencap/run/api.sock`. The daemon itself runs via `screencap serve` and is normally managed by a LaunchAgent installed by `screencap setup`.

When a CLI live-state command runs on a machine with no LaunchAgent installed (the headless / F3 install case), the CLI **auto-spawns** the daemon in the background via `posix_spawn` with `--idle-shutdown=600`. The auto-spawned daemon exits cleanly after 10 minutes of no requests, no event subscribers, and no active recording — preventing cron-driven `screencap status` from leaving permanent background processes. LaunchAgent-managed daemons omit the flag and run all day.

Auto-spawn diagnostic log: `~/.screencap/run/auto-serve.log` (mode 0o600).

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

## Key Patterns

- All user-facing output uses `rich.console.Console` (no bare `print()`).
- Heavy imports are deferred inside CLI command bodies to keep `screencap --help` fast.
- SQLite access in the `screencap` layer uses raw `sqlite3`, not SQLAlchemy.
- Recording dirs live at `~/.screencap/recordings/<name>/`.



## Documented Solutions

`docs/solutions/` — documented solutions to past problems (bugs, best practices, workflow patterns), organized by category with YAML frontmatter (`module`, `tags`, `problem_type`). Relevant when implementing or debugging in documented areas.

## Security

`SECURITY.md` at the repo root documents the daemon socket's trust boundary (same-EUID + filesystem permissions), in-scope and out-of-scope threats, and the SCR-64 decision rationale. It is the source of truth for threat-model questions.
