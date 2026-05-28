---
title: ".recording_ready.elapsed=0.0 when SIGTERM arrives during engine startup"
status: open
priority: high
created: 2026-05-28
related: SCR-69
---

# .recording_ready.elapsed=0.0 when SIGTERM arrives during engine startup

## Problem

For some clean stops the resulting `.recording_ready` reads:

```
{ "elapsed": 0.0, "completed_at": ..., "disk_full": false,
  "force_stopped": false, "terminated_reason": null }
```

Even though the engine ran for tens of seconds (audio captured, `profiling.json.duration_seconds` non-zero). The catalog renders `—` for duration because it reads `elapsed` from `.recording_ready` and falls back to a dash on `0.0` ([src/screencap/catalog.py:49](../../src/screencap/catalog.py)).

## Working hypothesis

`elapsed` in [src/screencap/engine/screen_recorder.py:834](../../src/screencap/engine/screen_recorder.py) is initialised to `0.0` and only updated *inside* the live loop:

```python
elapsed = 0.0
with Live(...) as live:
    try:
        while recorder.is_recording and not _stop_event.is_set():
            elapsed = time.time() - t0
            ...
```

The body must execute at least once to set `elapsed`. It is skipped entirely when either `recorder.is_recording` is `False` or `_stop_event` is set on the first check.

The only realistic way that happens for a recording that visibly ran:

1. SIGTERM arrives during engine startup, before `recorder.wait_for_ready(timeout=30)` returns.
2. The `SigtermOnly` handler at [screen_recorder.py:731](../../src/screencap/engine/screen_recorder.py) sets `_stop_event` *and* calls `recorder.stop()` → `_terminate_processing.set()`.
3. `wait_for_ready` is `self._ready_event.wait(timeout=timeout)` — it does **not** check `_stop_event`. The engine's outer "wait for all tasks to start" loop at [recorder.py:3472](../../src/screencap/engine/recorder.py) similarly ignores `terminate_processing`. So the main thread keeps blocking until `record.started` is finally emitted (or the 30s timeout).
4. When control returns and the live loop is entered, `recorder.is_recording` is already `False` (because `_terminate_processing` is set). The loop body never runs. `elapsed` stays at `0.0`.

Confirming evidence from a captured reproduction (`rec-20260528T094316`):

- dir created 09:43:16
- audio_0000.flac last modified 09:43:44 (~28 s of recording)
- profiling.json written 09:43:46 (`duration_seconds: 21.5`, `screen_event_count: 0`, `screenshot_avg_ms: 747.8` — TCC throttled)
- `.recording_ready` written 09:43:57

## Diagnostic dependency

The original SCR-69 PR added `serve.log` capture in the Debug-build `embed-cli.sh` launcher. With that in place, reproducing the scenario should show `record.started` emitting *after* the daemon's `proc.terminate()` audit entry — the prediction the hypothesis makes. If `record.started` lands first, the hypothesis is wrong and something inside the live loop is resetting `elapsed`.

## Proposed fix shape

Two changes, mutually compatible:

1. **Make `wait_for_ready` honour the stop signal.** Either pass `_stop_event` in (e.g. `wait_for_ready(timeout=30, abort_event=_stop_event)`) or wrap the call in a helper that races the two events. Return early when the worker has been asked to stop so the main thread stops blocking on `_ready_event`.
2. **Don't leave `elapsed = 0.0` when the live loop is skipped.** In the `finally` block of the Live `with`, set `elapsed = time.time() - t0` *if* `elapsed == 0.0` — captures the small wall-clock between entering the Live block and exiting. Pair with surfacing the engine's own recording duration via `RecordingResult` for the more-accurate value when available (engine writes `profiling.json` with the real duration; expose it back through `Recorder` so the worker can pick it up without re-reading disk).

The wider value of (2) is that `.recording_ready.elapsed` becomes correct even for unforeseen code paths where the loop is skipped, not just the SIGTERM-during-startup case.

## Acceptance

- A test that drives `_run_screen_recorder` with a `SigtermOnly`-style policy that fires SIGTERM **before** `record.started` is sent; the returned `RecordingResult.elapsed` is non-zero and approximately matches the engine's actual recording duration.
- A test that confirms `wait_for_ready` returns when `_stop_event` is set (or whichever shape the abort hook takes), without waiting the full 30 s timeout.
- The catalog Duration column shows a real number, not `—`, for the reproduction case (manual verification via `~/.screencap/recordings/<rec>/.recording_ready`).

## Out of scope

- The `recording_finalized` 30s timeout race; that is the body of SCR-69 itself and is fixed in this branch by raising the Swift in-app stop default to 60s.
- The TCC-degraded `screencapture` pacing (`screenshot_avg_ms=747.8`) — separate ticket; the elapsed=0.0 symptom reproduces regardless of the TCC issue once the SIGTERM-during-startup pattern is forced.
