---
title: "SIGINT handler installed too late — 30s window where signals are ignored"
slug: sigint-handler-timing-and-recording-stop-methods
date: 2026-03-18
category: runtime-errors
severity: high
problem_type: runtime-error
modules:
  - src/screencap/recorder.py
  - src/screencap/cli.py
tags:
  - sigint
  - sigterm
  - signal-handling
  - graceful-shutdown
  - orphaned-processes
  - recording-pipeline
symptoms:
  - "kill -INT <pid> has no effect — process ignores SIGINT entirely"
  - "Multiple SIGINT signals (3+) do not stop the recording process"
  - "Only SIGTERM kills the process, but with signal exit code (not graceful shutdown)"
  - "system_metrics.json contains end: null indicating unclean termination"
  - "Orphaned child processes with PPID=1 survive after parent is killed"
root_cause: >
  Custom SIGINT/SIGTERM handlers installed at line 771 of recorder.py, AFTER
  Recorder.__enter__() + wait_for_ready(30s) + chunk processor init — leaving a ~30s
  window with no custom handlers. Background processes inherit SIG_IGN for SIGINT from
  non-interactive shells, making SIGINT permanently ignored during this window.
---

# SIGINT Handler Installed Too Late — Recording Ignores Ctrl+C

## Problem

The recording process completely ignored `SIGINT` signals. Sending `kill -INT <pid>` three times had no effect. Only `SIGTERM` killed the process, but with a signal exit code (not graceful shutdown). `system_metrics.json` had `"end": null`. Orphaned child processes with `PPID=1` survived after the parent was killed.

## Root Cause

The custom SIGINT/SIGTERM handlers were installed **inside** the `with Recorder(...)` block, after a chain of setup steps that took up to ~30 seconds:

```
Line 648: Recorder.__enter__()     — starts threads + child processes
Line 653: wait_for_ready(30s)      — blocks main thread up to 30 seconds
Lines 656-690: Chunk processor     — initialization + start
Lines 694-703: PID file + child PIDs
Line 771: signal.signal(SIGINT, _force_exit)    <-- handler installed HERE
Line 780: signal.signal(SIGTERM, _sigterm_handler)
```

During this window, the signal disposition depended on the launch context:

| Context | SIGINT at startup | Behavior during setup window |
|---------|-------------------|------------------------------|
| Interactive terminal (foreground) | `default_int_handler` | Raises `KeyboardInterrupt` — process exits |
| Background via `&` in script/non-interactive shell | `SIG_IGN` (inherited) | **SIGINT silently ignored — process stays alive** |
| Launched by launchd/cron/agent | `SIG_IGN` (inherited) | Same — silently ignored |

**Evidence confirming the root cause:**
1. SIGTERM killed with a signal exit code (not graceful) — SIGTERM handler at line 780 was also not installed
2. `system_metrics.json` had `"end": null` — no cleanup path ran
3. Three `kill -INT` calls had no effect — SIGINT was `SIG_IGN`, not the default Python handler

## Solution

**Two-phase handler approach**: install handlers early with guards for uninitialized variables, populate state later.

### Edit 1: Pre-initialize closure variables (line ~598)

```python
# Pre-initialize for signal handler closures (handlers installed before
# Recorder.__enter__ so signals work during the entire setup window).
recorder = None
_child_pids = []
_ctrl_c_count = 0
```

### Edit 2: Move handler definitions before `with Recorder(...)` (line ~654)

Both `_force_exit` and `_sigterm_handler` are defined and installed via `signal.signal()` **before** the `with Recorder(...)` statement. Each handler contains guards:

- `_force_exit`: Guards `recorder.stop()` with `if recorder is not None`. Falls back to `multiprocessing.active_children()` when `_child_pids` is empty:
  ```python
  _pids_snapshot = (
      list(_child_pids) if _child_pids
      else [c.pid for c in multiprocessing.active_children()]
  )
  ```
- `_sigterm_handler`: Guards `recorder.stop()` with `if recorder is not None`.

### Edit 3: Remove old handler definitions from inside the `with` block

The old handler definitions at the previous lines 705-780 were removed. `_child_pids` is still populated inside the `with` block (line 793) once child processes are spawned.

## All Ways to Stop a Recording

This investigation clarified all 8 stop mechanisms. Understanding these is critical for anyone working on the recording pipeline.

### Signal-based (external)

| # | Trigger | Signal | Handler | Cleanup |
|---|---------|--------|---------|---------|
| 1 | **1st Ctrl+C** or `kill -INT` | SIGINT | `_force_exit` | **Full graceful**: sets `_stop_event`, calls `recorder.stop()`, normal `finally` block runs (metrics, sentinel, PID file, handler restore) |
| 2 | **2nd Ctrl+C** or `kill -INT` | SIGINT | `_force_exit` | **Partial**: SIGTERM then SIGKILL to children, writes sentinel, deletes PID file, `os._exit(1)` — skips `finally` blocks |
| 3 | **3rd+ Ctrl+C** | SIGINT | `_force_exit` | **None**: immediate `os._exit(1)` |
| 4 | **`screencap stop`** or `kill -TERM` | SIGTERM | `_sigterm_handler` | **Full graceful**: identical to #1 |
| 5 | **`screencap stop --force`** | Finds orphans, SIGKILL | N/A (external) | **Brute force**: processes killed, PID file deleted by stop command |
| 6 | **`kill -9`** | SIGKILL | Unhandleable | **None**: children orphaned (PPID=1), PID file remains |

### Automatic (internal)

| # | Trigger | Mechanism | Cleanup |
|---|---------|-----------|---------|
| 7 | **Disk full** (free < `stop_mb`) | `_stop_event.set()` + `recorder.stop()` in main loop | **Full graceful** |
| 8 | **Critical child crash** | `recorder.is_recording` becomes False, detected in main loop | **Full graceful** (minus the crashed child's data) |

### `screencap stop` fallback chain (`cli.py:861-921`)

1. Reads PID file, validates parent PID via `_is_screencap_process()`
2. Sends `SIGTERM` to parent process
3. Polls for up to 30 seconds (60 x 0.5s)
4. If timeout → falls through to `find_orphaned_processes()` → SIGKILL
5. Deletes PID file

### Key decision: `screencap stop` is the recommended stop method

- Works from any context (different terminal, script, agent, cron)
- Has a built-in fallback chain (SIGTERM → orphan detection → SIGKILL)
- Handles PID file cleanup
- **Ctrl+C is a convenience shortcut for foreground terminal use only**

## Prevention

### Tests added (AST-based structural tests)

1. **`test_signal_handler_installed_before_recorder_enter`** — Verifies `signal.signal(SIGINT, ...)` appears before `Recorder(` in the AST of `start_recording()`
2. **`test_force_exit_guards_recorder_stop`** — Verifies every `recorder.stop()` in `_force_exit` is inside an `if recorder is not None` guard
3. **`test_force_exit_fallback_to_active_children`** — Verifies `multiprocessing.active_children()` is called as fallback in `_force_exit`
4. **`test_sigterm_handler_guards_recorder_stop`** — Same None guard check for `_sigterm_handler`

### Principles

1. **Install signal handlers as early as possible, before any long-running setup.** Non-interactive contexts inherit `SIG_IGN` for SIGINT, which stays permanently unless overridden by `signal.signal()`.
2. **Closures referencing not-yet-bound variables need None guards.** When a handler is installed early, variables like `recorder` may not be bound yet. Guard every reference.
3. **Use `screencap stop` instead of raw signals.** Tools, skills, and agents should use `screencap stop` — it validates PIDs, has fallbacks, and handles cleanup.

### Historical context — four iterations of signal handling

| # | Date | Change | Problem it introduced |
|---|------|--------|-----------------------|
| 1 | Original | `os._exit(1)` on 2nd Ctrl+C | Orphaned all children |
| 2 | 2026-02-20 | `sys.exit(1)` + child cleanup | Parent hung in `threading._shutdown()` deadlock |
| 3 | 2026-03-08 | Back to `os._exit(1)`, stored PIDs, `os.kill()` | Handler installed too late (30s window) |
| 4 | 2026-03-18 | Moved handler before `Recorder.__enter__()` | (current — no known issues) |

## Related Documentation

- [Ticket: SIGINT handler not triggering](../../epics/02-recording-pipeline/tickets/2026-03-18-fix-sigint-handler-not-triggering.md)
- [Ticket: Slow shutdown from queue draining](../../epics/02-recording-pipeline/tickets/2026-02-22-fix-slow-shutdown-queue-draining.md)
- [Ticket: Parent process hangs after force-quit](../../epics/02-recording-pipeline/archived/tickets/high-2026-03-08-parent-process-hangs-after-force-quit.md)
- [Ticket: Orphan detection fails for spawn workers](../../epics/02-recording-pipeline/tickets/high-2026-03-10-fix-orphan-detection-spawn-workers.md)
- [Research: Institutional learnings — process cleanup](../../epics/02-recording-pipeline/research/INSTITUTIONAL_LEARNINGS_PROCESS_CLEANUP.md)
- [Plan: Daemon mode and stop command](../../epics/02-recording-pipeline/plans/2026-02-20-feat-daemon-mode-stop-command-plan.md)
- [PR #98](https://github.com/proteus-computer-use/screencap/pull/98)
