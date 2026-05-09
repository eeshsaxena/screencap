---
title: "Store start/stop Task handles in RecorderController and cancel on deinit"
status: open
priority: medium
created: 2026-05-08
related_plans:
  - docs/plans/2026-05-08-001-feat-daemon-architecture-phase-1-plan.md
related_review_run: /tmp/compound-engineering/ce-code-review/20260508-210223-e214ede7/
---

# Store start/stop Task handles in RecorderController and cancel on deinit

## Problem

In [macos/ScreenCap/Controllers/RecorderController.swift:180](../../macos/ScreenCap/Controllers/RecorderController.swift) and the `runStop` path, the daemon dispatch is:

```swift
Task { await startViaDaemon(name: name) }
```

The `Task` handle is not stored anywhere. Two concrete problems:

1. **Lifetime leak.** If `stop()` is called before the daemon RPC returns, `attachDaemonEventStream()` and `startPermissionWatchdog()` still execute after state has returned to `.idle`. `consumeDaemonEvents` exits immediately (its `state.isRecording` guard fails), but the permission watchdog timer is armed and ticks every 5 seconds until the next `stopPermissionWatchdog()` call. Battery drain and confusion.
2. **No traceable error propagation.** Errors thrown inside the Task are swallowed silently. The existing `daemonEventTask` pattern is the correct counter-example: that one IS stored.

## What's needed

1. Add `private var startTask: Task<Void, Never>?` and `private var stopTask: Task<Void, Never>?` properties.
2. Replace fire-and-forget Task dispatch with stored-handle dispatch. Cancel the previous handle on re-entry.
3. In `deinit`, cancel both handles.
4. After `await DaemonClient.recordingStart(...)` in `startViaDaemon`, add `guard state == .starting else { return }` to avoid arming the permission watchdog if state has already moved away from `.starting`.
5. Same pattern for `runStop` if applicable.

## Acceptance

- Swift compile clean under `SWIFT_STRICT_CONCURRENCY: complete`.
- New test exercising the cancel-mid-flight scenario: dispatch start, immediately call stop, assert no watchdog activity after the start Task is cancelled.
- Manual smoke: rapid start/stop on the SwiftUI app, no extra activity in Activity Monitor 30s after.

## References

- Tier-3 ce-code-review run artifact: `/tmp/compound-engineering/ce-code-review/20260508-210223-e214ede7/`
- Reviewers: reliability + swift-ios (cross-reviewer, confidence 100, P2)
