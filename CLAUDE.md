# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

ScreenCap is a macOS CLI for screen recording. The recording engine lives at `src/screencap/engine/` as an internal sub-package. Python >= 3.10, macOS only.

A native SwiftUI app shell lives at `macos/` and wraps the bundled CLI; see "macOS SwiftUI app shell" below for details.

### Daemon architecture (Phase 2)

The recording engine is supervised by a background daemon. CLI live-state commands (`screencap start` / `stop` / `status`) are thin HTTP clients of the daemon's `/v0/*` API over a UNIX socket at `~/.screencap/run/api.sock`. The daemon itself runs via `screencap serve` and is normally managed by a LaunchAgent installed by `screencap setup`.

When a CLI live-state command runs on a machine with no LaunchAgent installed (the headless / F3 install case), the CLI **auto-spawns** the daemon in the background via `posix_spawn` with `--idle-shutdown=600`. The auto-spawned daemon exits cleanly after 10 minutes of no requests, no event subscribers, and no active recording — preventing cron-driven `screencap status` from leaving permanent background processes. LaunchAgent-managed daemons omit the flag and run all day.

Auto-spawn diagnostic log: `~/.screencap/run/auto-serve.log` (mode 0o600).

### Unified recording processing pipeline

Recordings flow through one **disk-first pipeline** rather than a fork at recording start. Capture writes rich chunks to disk as the source of truth; destination-agnostic stages run once per chunk; a single terminal stage converges each recording toward its destination.

- **On-disk per-chunk ledger** — `src/screencap/pipeline_state.py` (`PipelineLedger`) persists per-chunk lifecycle state in a `pipeline_chunk_state` table inside the local-only `recording.db`. It is the on-disk replacement for the old in-memory `chunk_processor._chunk_results`, re-establishing the five data-loss prevention rules on disk (closed-set seeding at rotation, tri-state upload state where `SKIPPED` ≠ `UPLOADED` ≠ `FAILED`, never-delete-without-fresh-remote-confirm, frozen `chunks_expected`, crash-safe `EVICT_PENDING` ordering). The ledger is written cross-process with `busy_timeout=10000` + `BEGIN IMMEDIATE` per transition.
- **Destination-agnostic stages** — `src/screencap/pipeline_stages.py` (`PipelineStageRunner`) runs transcribe → export events → manifest once per chunk, idempotently (ledger `STAGED` + on-disk artifact existence), with no local-vs-cloud knowledge.
- **Policy resolver (monetization seam)** — `src/screencap/pipeline_policy.py` (`resolve_policy` → frozen `ResolvedPolicy{destination, retention_policy, params}`), frozen per recording into `.recording_intent` (schema v2) at start time; `set_default_override` is the single future plan-tier attachment point. `config.get_retention_policy()` is the default; `keep_forever` is the default policy (R11).
- **Terminal stage** — `src/screencap/terminal_stage.py` (`run_terminal_stage`) is the single disk-driven, idempotent convergence point. It acquires a per-recording advisory `fcntl.flock` (`~/.screencap/run/terminal-<name>.lock`) FIRST on every entry point, reconciles the ledger against GCS, routes by the frozen policy (local → no upload; cloud/both → scrubbed/masked copy via the `CloudCopyProducer` scrub-seam adapter, upload everything except `recording.db`), and writes the completeness sentinel last, gated on the frozen `chunks_expected`.
- **Universal retention & eviction** — `src/screencap/retention.py` (`evict_recording`) is decoupled from upload; the hard floor (only `UPLOADED` cloud / `LOCAL_DONE` local chunks are candidates; cloud deletes re-confirm remote NOW) holds under every policy.
- **`recording.db` is local-only by rule** — never uploaded (`upload.list_recording_files` excludes it + sidecars + `*.scrub_failed`; `upload.assert_uploadable` hard-rejects it). Cloud structured data derives only from scrubbed exports.
- **Cloud video privacy** is governed by the `masked_video_upload` flag (default OFF). See `SECURITY.md` for the capture-time-blocking-vs-post-hoc-masking trust boundary and the prerequisites for enabling it.

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
