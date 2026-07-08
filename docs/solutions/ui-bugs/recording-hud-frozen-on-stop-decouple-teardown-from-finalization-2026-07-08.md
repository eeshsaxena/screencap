---
title: "Recording HUD pill froze on Stop: decouple UI teardown from the background finalization await"
date: 2026-07-08
category: ui-bugs
module: macos-app-shell
problem_type: ui_bug
component: macos-app-shell
symptoms:
  - "Clicking 'Stop & save' on the floating recording HUD pill leaves it frozen on screen (with a stalled elapsed clock) instead of disappearing"
  - "The pill lingers for up to 60 seconds before it finally vanishes"
  - "The user is stuck in a 'still stopping' limbo while the recording finalizes in the background"
root_cause: async_timing
resolution_type: code_fix
severity: medium
related_components:
  - recording-state-machine
  - stop-policy
tags:
  - macos
  - swiftui
  - state-machine
  - recording-lifecycle
---

# Recording HUD pill froze on Stop: decouple UI teardown from the background finalization await

## Problem

Clicking **"Stop & save"** on the floating recording HUD pill (`RecordingHUDPanel`) left it frozen on screen — elapsed clock stalled — for up to 60 seconds instead of dismissing immediately, blocking the user's workflow while the recording finalized in the background.

## Symptoms

- The pill stays visible after Stop is clicked; its clock is frozen at the last tick (`tickElapsed` is a no-op outside `.recording`).
- It disappears only once background finalization completes — or after the 60s wall-clock fallback times out.

## Root cause

In the recording state machine (`macos/ScreenCap/Controllers/RecordingStateMachine.swift`, driven by `RecorderController.swift`), the pill's teardown is modeled as a `.hideHUD` side-effect. That effect was **only** emitted by the terminal transition `enterIdle()`, which runs inside `finalizeStop()` — and `finalizeStop()` is called *after* `runStop()` `await`s the `recording_finalized` event with a deliberate 60s wall-clock fallback (`LiveStopPolicyCoordinator.inAppStopTimeout`, kept at 60s to avoid re-introducing SCR-69's false "still finalizing" toast).

`enterStopping()` — the transition that fires the instant Stop is clicked — returned no effects. So nothing tore the pill down until the async finalization await resolved. **The HUD's on-screen lifetime was welded to the background finalization wait.**

## Solution

Decouple the *visual* teardown from the async finalization await: emit the HUD-dismiss effect on the **user-action edge**, and keep the background await purely for error-surfacing / force-kill-on-timeout.

Before — HUD hide only reachable after the await:

```swift
// enterStopping: no effects → pill stays up
case .recording:
    state = .stopping(quitting: quitting)
    return []

// finalizeStop (runs AFTER `await stopPolicy.runStop(...)`):
apply(machine.enterIdle())   // [.hideHUD, .restoreMainWindow] — the ONLY hide
```

After — hide synchronously on Stop; terminal transition concludes in the background:

```swift
// enterStopping: in-app Stop drops the pill immediately
case .recording:
    state = .stopping(quitting: quitting)
    return quitting ? [] : [.hideHUD]   // Cmd+Q keeps its UI + countdown

// finalizeStop, non-quitting path:
apply(machine.enterIdleStayingBackgrounded())
// → [.hideHUD, .endRecordingInBackground]
// routes the still-hidden window to Library WITHOUT restore/activate (no focus steal)
```

Two supporting changes keep the invariants intact:

- `restoreRecordingAfterStopFailure()` now returns `[.showHUD]` — if the stop signal fails to dispatch, the recording is still live, so the pill (dropped on `enterStopping`) must come back.
- Cmd+Q (`quitting: true`) is untouched: it keeps the recording UI up while its quit-progress countdown runs (rendered in the banner / menu bar), and its terminal `enterIdle()` still restores + activates the window.

## Why this works

`stop()` applies `enterStopping`'s effects **synchronously**, before the `Task { runStop() }` is even scheduled — so the pill is gone the instant the user clicks, while the 60s finalization wait (and SCR-69's fix) is entirely preserved. Only the moment the UI lets go moved; finalization still runs to completion in the background for its error/force-kill outcomes.

## Prevention

**In an effect-emitting state machine — the `(state, event) -> (newState, [Effect])` pattern — never gate a user-visible UI-teardown effect (HUD dismiss, window close, spinner stop) behind a transition that only fires after a long-running background `await` (finalization, upload, network round-trip).** The UI freezes for the full duration of that wait.

- Emit visual state transitions on the **user-action edge** (here, `enterStopping`), not on the async-completion edge.
- Let the background `await` drive only error / retry / cleanup outcomes — not the visible dismissal.
- Regression guard: assert the teardown effect fires **synchronously** after the user action, before the async work resolves. The controller test `testInAppStopDismissesHUDImmediatelyAndStaysBackgrounded` asserts `hideHUDCount == 1` right after `stop()` (and `restoreMainWindowCount == 0` through to idle).

This area (the recording stop lifecycle) has a history of subtle bugs — SCR-69 (false "still finalizing" toast), SCR-71 (`.recording_ready.elapsed=0`), SCR-100 / SCR-101 (capture-health teardown) — so keep visual and finalization concerns explicitly separated.
