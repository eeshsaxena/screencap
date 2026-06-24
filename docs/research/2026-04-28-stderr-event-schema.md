---
date: 2026-04-28
researcher: claude
topic: "Structured stderr event schema — the cross-language contract"
tags: [research, codebase, stderr-events, contract, swiftui, upload, recorder]
status: living
last_updated: 2026-06-22
last_updated_by: claude
---

# Structured stderr event schema (cross-language contract)

This is the cross-language contract referenced by `src/screencap/_stderr_events.py`,
`src/screencap/cli/__init__.py`, and `tests/test_stderr_event_contract.py`. The
Python side emits these events; the SwiftUI shell (`RecorderController.spawn`,
`UploadController`) parses them off the subprocess's **stderr** to drive UI state
transitions.

This doc is **derived from the source of truth** — the event-type constants and
`__all__` in `_stderr_events.py`, the field shapes pinned by
`tests/test_stderr_event_contract.py`, and the emit sites in `cli/__init__.py`,
`session.py`, `engine/screen_recorder.py`, and `upload.py`. When those change,
update this doc and (for a breaking field change) bump `EVENT_SCHEMA_VERSION`.

## Wire format

`emit_event(type, **fields)` writes a single JSON object per line to `sys.stderr`
and flushes immediately (SwiftUI's reader is line-buffered). Every payload
carries three baseline fields injected by `emit_event`:

| field            | type   | notes                                                     |
| ---------------- | ------ | --------------------------------------------------------- |
| `type`           | string | the event-type discriminator (required to decode)          |
| `ts`             | float  | `time.time()` at emit                                     |
| `schema_version` | int    | `EVENT_SCHEMA_VERSION` (currently `1`)                    |

Event-specific fields are appended via `**fields`. Emit failures are swallowed
(a broken stderr must never break the recorder).

### Tolerant-reader contract

The decoder is **drift-resilient**: unknown event `type`s are ignored, unknown
fields are dropped, and non-JSON / blank lines (e.g. Rich progress-bar bleed)
return nil rather than poisoning the stream. Consequence: **adding a new event
type or a new field never breaks an existing consumer.** Only a consumer that
wants to *read* a new field needs a code change. This is why new events
(`upload_busy`, below) ship as additive, non-breaking changes.

Streams are separated by rule: events go to **stderr only**; stdout stays clean
for human/machine output (`tests/test_stderr_event_contract.py::TestStreamSeparation`).

## Exit codes

The terminal `exit_code` on the `stopped` event matches the process exit code:

| code | meaning                |
| ---- | ---------------------- |
| 0    | clean                  |
| 1    | generic failure        |
| 2    | lock-held              |
| 3    | permission denied/lost — `permission_required` (start-time block) OR `permission_lost` (mid-recording) |
| 4    | disk_full              |
| 5    | user-initiated force-quit |

## Active events (emitted in v1)

Recorder / session lifecycle, daemon bus, and upload-pipeline events. Field
shapes below are pinned by `tests/test_stderr_event_contract.py` where a test
exists, and otherwise read from the emit site.

### Recorder / session lifecycle

- **`started`** — recording session began. Fields: `claimant` (`"cli"` /
  `"swiftui"` / `"daemon"`). Post-todo-010 it does **not** carry `capture_dir`
  (the per-recording dir isn't allocated yet; consumers learn the path from
  `recording_finalized.name` + `screencap status --json`).
- **`lock_contended`** — `screencap start` found the lock held by another
  claimant. Fields: `owner` (object with `pid: int`, `claimant: "cli"|"swiftui"`).
- **`recording_finalized`** — a recording finished and was written out. Fields:
  `name: str`, `duration_seconds: float`, `force_stopped: bool`, `disk_full: bool`.
- **`disk_full`** — capture stopped because the disk filled. Fields: `name: str`,
  `capture_dir: str`. (Terminal; maps to exit 4.)
- **`permission_lost`** — a TCC permission was revoked mid-recording. Fields:
  `permission` (one of `screen_recording` / `accessibility` / `input_monitoring`),
  `elapsed: float`. Only a `screen_recording` denial is terminal (exit 3);
  `accessibility` / `input_monitoring` are best-guess attributions (SCR-101).
- **`permission_required`** (SCR-142) — a **start-time** permission block: the
  daemon's pre-spawn permission gate rejected the start and `screencap start`
  re-emits the rejection as a structured event so the CLI-fallback shell can
  name the exact missing permission(s). Unlike single-permission
  `permission_lost`, it carries `missing: list[str]` — the full set of denied
  permissions (each a subset of `screen_recording` / `accessibility` /
  `input_monitoring`). Terminal; maps to exit 3. Additive/non-breaking (a new
  event type with a field absent on all others), so `EVENT_SCHEMA_VERSION` is
  unchanged.
- **`capture_unhealthy`** (SCR-76) — **advisory**, no exit code, never terminal.
  Emitted once per detection edge when a reader is demonstrably attempting but
  producing no useful output and the cause is not a `screen_recording` denial.
  Fields: `reason` (closed set: `reader_stalled` / `listener_dead`),
  `reader` (`screen` / `window` / `action`), `elapsed: float`. The `reason` field
  is a CLOSED set of constants — runtime-derived text must never be interpolated
  into it (it rides the daemon EventBus to any same-EUID subscriber).
- **`capture_recovered`** (SCR-100) — **advisory**, no exit code, never terminal.
  Paired clear for `capture_unhealthy`: emitted once when a previously-unhealthy
  reader returns healthy mid-recording, so the shell drops the stale advisory.
  Fields: `reader` (`screen` / `window` / `action`), `elapsed: float`. Carries
  **no** `reason` (unlike `capture_unhealthy`).
- **`stopped`** — recording session ended. Fields: `exit_code: int` (see table).
- **`menubar_neutralized_by_env`** — an env var prevented menubar spawn. Fields:
  `env: str` (the env var name, e.g. `SCREENCAP_DISABLE_MENUBAR`).
- **`matrix_disclosure_required`** — emitted when a SwiftUI parent has not
  acknowledged a policy-matrix change. Fields: `changes` (list of string keys).
- **`lock_metadata_write_failed`** — best-effort lock-metadata write failed
  (emitted from `session.py`); advisory.
- **`terminated_reason_persist_failed`** — best-effort terminated-reason persist
  failed (emitted from `engine/screen_recorder.py`); advisory.

### Daemon bus events

Published by the daemon (not the engine subprocess), riding the same schema
version and JSON shape so tolerant clients can handle them on the same taxonomy:
`engine_crashed`, `previous_session_recovered`, `previous_session_force_terminated`,
`subscribed`.

### Upload pipeline events (plan U1)

Emitted from `upload.py` and `cli/__init__.py`'s `upload` command; consumed by
the SwiftUI review window's `UploadController` to drive progress UI.

- **`upload_started`** — fields: `recording: str`, `file_count: int`,
  `total_bytes: int`.
- **`upload_file_done`** — fields: `recording: str`, `name: str`,
  `bytes_uploaded_so_far: int`, `files_done: int`, `files_total: int`.
- **`upload_finished`** — terminal success. Fields: `recording: str`,
  `uploaded: int`, `skipped: int`, `failed: int`, `total_bytes: int`,
  `gcs_prefix: str`.
- **`upload_failed`** — terminal failure. Fields: `recording: str`, `error: str`,
  and (on partial paths) `uploaded` / `failed` / `total_bytes`. **SCR-79 pins
  `upload_failed` to a non-zero exit** — it is never used for an exit-0 outcome.
- **`upload_busy`** (SCR-158) — **terminal, retryable, NON-failure** outcome.
  `screencap upload` skipped a recording because another *process* held the
  per-recording terminal-stage lock (a finalize, a daemon resume, or a concurrent
  upload). Exit stays **0**. Fields: `recording: str`, `retryable: bool` (always
  `true` in v1). Deliberately distinct from `upload_failed` (which SCR-79 ties to
  a non-zero exit): the event-first Swift `UploadController` maps it to a
  retry-friendly `.busy` state rather than rendering exit-0-without-a-terminal-event
  as a hard failure, and an autonomous agent reads `retryable` to tell a busy-skip
  apart from a successful no-op without scraping console text.

## Reserved events (schema documented, NOT emitted in v1)

- **`chunk_finalized`** (todo 004) — per-chunk finalize notification. Wiring is
  deferred to a follow-up that touches `chunk_processor.py`. The `emit_event`
  helper still serializes the documented payload (`chunk_index: int`, `path: str`)
  for any future emission, but **no production site fires it in v1** (guarded by
  `tests/test_stderr_event_contract.py::test_chunk_finalized_not_emitted_in_production`).
  SwiftUI consumers should treat its absence as informational, not authoritative —
  do not block on it.
