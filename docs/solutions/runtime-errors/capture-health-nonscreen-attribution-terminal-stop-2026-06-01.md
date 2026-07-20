---
title: "Capture-health treated a best-guess non-screen TCC attribution as terminal and self-stopped a healthy recording"
date: 2026-06-01
category: runtime-errors
problem_type: runtime_error
component: screencap.engine.recorder
platform: macos
severity: high
symptoms:
  - "A healthy screen+audio recording stops mid-session: \"Recording stopped: accessibility was revoked\""
  - "Capture-health emits terminal `permission_lost(accessibility)` while Screen Recording is granted and capturing fine"
  - "Users who never granted Accessibility see recordings torn down on any transient window-reader stall"
  - "SCR-76's \"emit-only / fail-open, never self-stops\" invariant is violated for non-screen permissions"
root_cause: >
  _emit_capture_health_event routed ANY non-None TCC label from the best-guess
  in-process labeller (_probe_tcc_denied) to the terminal-capable permission_lost
  event, which the SwiftUI shell turns into stop(). Window-reader output is
  CGWindowList-gated (Screen Recording), not Accessibility-gated, so an
  "accessibility" label on a window stall is a coincidental denial, not the cause
  — and acting on that guess terminally stopped an otherwise-healthy recording.
resolution_type: code_fix
modules:
  - src/screencap/engine/recorder.py
  - src/screencap/engine/window/_macos.py
  - src/screencap/_stderr_events.py
  - macos/Screencap/Controllers/RecorderController.swift
tags:
  - macos
  - tcc
  - capture-health
  - permission-attribution
  - fail-open
  - accessibility
  - scr-101
  - stderr-events
---

# Capture-health treated a best-guess non-screen TCC attribution as terminal and self-stopped a healthy recording

## Problem

SCR-76 added a capture-health detector to the engine's 1s supervisor loop. It tracks per-reader (screen/window/action) attempt-vs-output counters, debounces a stall (~10s warmup plus 3 ticks), attributes the cause in-process via `_probe_tcc_denied`, and emits a structured stderr event. The design's stated invariant is emit-only / fail-open: the detector reports health but never self-stops a recording.

`_emit_capture_health_event` (`src/screencap/engine/recorder.py`) broke that invariant. It routed ANY non-None TCC label to the terminal `permission_lost` event:

```python
if label is not None:
    emit(EVENT_PERMISSION_LOST, permission=label, elapsed=elapsed)
    return EVENT_PERMISSION_LOST
```

`permission_lost` is terminal by contract. The Swift shell's `handlePermissionLost` (`macos/Screencap/Controllers/RecorderController.swift:576`) calls `stop()` on ANY permission while in the `.recording` state:

```swift
if case .recording = state {
    stop()
}
```

So a non-screen permission label flowing through `permission_lost` tears down a live recording.

## Symptoms

A healthy screen+audio recording stops itself mid-session, surfacing "Recording stopped: accessibility was revoked" even though the user never touched Accessibility and screen capture was working fine.

Trigger conditions, all common in normal use:

- The window reader stalls for the debounce window (a momentary `CGWindowListCopyWindowInfo` lull, a transition with no queryable top window, etc.), AND
- Accessibility happens to be independently denied (many users record their screen without ever granting Accessibility).

`_probe_tcc_denied` biases its check ORDER by reader. For the window reader it checks Accessibility first:

```python
order = {
    "screen": (_screen, _input, _ax),
    "window": (_ax, _screen, _input),
    "action": (_input, _ax, _screen),
}.get(reader, (_screen, _input, _ax))
```

So a window stall plus a denied Accessibility permission yields the label `"accessibility"`, which the old code promoted to terminal `permission_lost`, which the shell turns into a `stop()`.

## What Didn't Work

The originating SCR-101 finding reached the right conclusion (a non-screen permission can stop a healthy recording) on a wrong mechanism. It claimed: "window data is AX-gated, so under Accessibility denial `get_active_window_data()` is deterministically falsy on every poll, stalling the window reader."

Verifying the full causal chain against the code refuted that mechanism:

- The window reader increments `window.output` whenever `get_active_window_data()` is truthy (`src/screencap/engine/recorder.py` `read_window_events`, ~1593-1597).
- `get_active_window_data()` builds its dict from window meta sourced via `CGWindowListCopyWindowInfo` (`src/screencap/engine/window/_macos.py:208`), which is Screen-Recording-gated, NOT Accessibility-gated.
- The only Accessibility-dependent part is the optional deep a11y `data` subtree (`get_window_data`), and an empty `data` does NOT make the returned dict falsy (`src/screencap/engine/window/__init__.py` `get_active_window_data`, ~27-60, returns a populated meta dict regardless).

So Accessibility denial does not cause the window reader to stall. Building a fix around "AX-causes-stall" would have been wrong. The real defect is misattribution: `_probe_tcc_denied`'s per-reader label is a best-GUESS, and the bug was treating a non-screen guess as terminal.

The existing tests did not catch this. `test_accessibility_denied_labels_accessibility` and `test_reader_bias_picks_most_likely_when_multiple_denied` blessed the labeller's window→accessibility mapping IN ISOLATION with fake TCC. None encoded the invariant "a non-screen permission must not terminate a healthy recording," so the terminal-routing defect lived downstream of every test.

## Solution

Make only a `screen_recording` denial terminal. Everything else is the advisory, non-terminal `capture_unhealthy` event.

Before:

```python
if label is not None:
    emit(EVENT_PERMISSION_LOST, permission=label, elapsed=elapsed)
    return EVENT_PERMISSION_LOST
```

After (`src/screencap/engine/recorder.py` `_emit_capture_health_event`):

```python
if label == "screen_recording":
    emit(EVENT_PERMISSION_LOST, permission=label, elapsed=elapsed)
    return EVENT_PERMISSION_LOST
reason = (
    CAPTURE_UNHEALTHY_REASON_LISTENER_DEAD
    if reader == "action"
    else CAPTURE_UNHEALTHY_REASON_READER_STALLED
)
emit(EVENT_CAPTURE_UNHEALTHY, reason=reason, reader=reader, elapsed=elapsed)
return EVENT_CAPTURE_UNHEALTHY
```

A `None`, `accessibility`, or `input_monitoring` label now falls through to advisory `capture_unhealthy` with a closed-set `reason` (`listener_dead` for the action reader, `reader_stalled` otherwise). The contract docstrings in `src/screencap/_stderr_events.py` were reconciled to state that only a `screen_recording` denial reuses the terminal `permission_lost`.

## Why This Works

The fix reserves the destructive effect for the one signal that is authoritative. A `screen_recording` denial means the core screen capture is genuinely dead, so tearing down the recording is correct.

A genuine screen-recording loss is still caught terminally and independently of the window path: the SCREEN reader's `_probe_tcc_denied("screen")` checks `_screen` first in its order tuple, so when screen capture actually dies, the screen reader stalls and produces the terminal `permission_lost` on its own. No legitimate teardown is lost by demoting the window/action labels.

For window/action stalls, the demotion is correct because those readers' useful output does not ride Accessibility:

- Window output rides `CGWindowListCopyWindowInfo` (Screen Recording), so an `accessibility` label there is a coincidental denial, not the cause of the stall.
- Action liveness rides the input listener (Input Monitoring), independent of Accessibility too.

Routing those guesses to advisory `capture_unhealthy` notifies the shell without stopping the recording, restoring SCR-76's emit-only / fail-open invariant. Tolerant SwiftUI parsers ignore the advisory if they have not learned it, so there is no teardown side effect either way.

## Prevention

New tests pin the invariant directly:

- `tests/test_capture_health.py` — `test_only_screen_recording_label_is_terminal`, `test_accessibility_label_emits_advisory_not_terminal`, `test_input_monitoring_label_emits_advisory_listener_dead` (unit-level on `_emit_capture_health_event`).
- `tests/test_capture_health_frozen_dispatch.py` — `test_nonscreen_attribution_stays_advisory_not_terminal` (end-to-end through the real `_capture_health_tick` wiring, closing the green-in-tests / broken-in-frozen gap rather than testing the labeller in isolation).

General guardrails:

- **A heuristic best-guess attribution must never drive a terminal or destructive action.** Reserve terminal effects for the one authoritative signal (here, a `screen_recording` denial detected by the screen reader, which checks screen first).
- **When a code-review finding states a mechanism, verify the FULL causal chain against the code before fixing.** A right conclusion can rest on a wrong mechanism, and fixing the wrong mechanism leaves the real defect active.
- **Map which macOS TCC permission actually gates each capture path** rather than assuming by reader name: window meta = `CGWindowListCopyWindowInfo` = Screen Recording; deep a11y `data` subtree = Accessibility; input/gesture listeners = Input Monitoring. Do not assume "window implies Accessibility."

## Related

- `docs/plans/2026-05-29-002-feat-scr-76-capture-health-detection-plan.md` — designed this detector. Its truth table mapped window stalls to terminal `permission_lost(accessibility)`; that row is **superseded** by this fix (only `screen_recording` is terminal), and the bug is the realization of the plan's own open question about `handlePermissionLost` teardown on the daemon path.
- `docs/solutions/runtime-errors/macos-tcc-per-process-cache-quit-and-relaunch.md` — the per-process TCC cache limitation that makes mid-recording attribution inherently best-guess; the foundation for why window/action attribution should not be terminal.
- `docs/solutions/runtime-errors/eventbus-late-listener-replay-2026-05-12.md` — the engine → stderr → EventBus → `RecorderController` path that carries `permission_lost` to the terminal consumer.
- Linear SCR-101 (parent SCR-76); fixed in PR #201 (proteus-computer-use/screencap).
