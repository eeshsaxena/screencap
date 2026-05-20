---
title: "Add focused test for Cmd+Q SIGKILL branch in RecorderController.runStop"
status: open
priority: medium
created: 2026-05-20
source: code-review PR #185 (finding #6)
related_plans:
  - docs/plans/2026-05-19-001-refactor-split-recorder-controller-plan.md
related_review_run: /tmp/compound-engineering/ce-code-review/20260520-7f1f8961/
---

# Add focused test for Cmd+Q SIGKILL branch in RecorderController.runStop

## Problem

Plan U3 listed "Cmd+Q timeout, CLI alive → invoke `killProcess` closure" as a required test scenario. `StopPolicyCoordinatorTests` correctly verifies the coordinator returns `.timedOut` after the 300s race, but the SIGKILL decision actually lives in `RecorderController.runStop`:

```swift
if let process = cliService.currentProcess, process.isRunning, process.processIdentifier > 0 {
    kill(process.processIdentifier, SIGKILL)
}
```

That branch — the `isRunning && pid > 0` guard and the syscall — has no focused test. If the guard regresses (e.g., `process.isRunning` removed because "the process is always running here"), or if the SIGKILL is invoked against a recycled PID after the process exited mid-await, no automated test catches it. This is the most consequential branch in the stop path; a regression here orphans a child process or signals an unrelated user-owned process.

## What's blocking a direct test

`CLIClient.SpawnedProcess` is a concrete `final class` wrapping `Foundation.Process`. The orchestrator reads `process.isRunning` and `process.processIdentifier` directly. To test the SIGKILL branch end-to-end with a fake CLI service, the test needs to inject a `currentProcess` whose `isRunning` and `processIdentifier` are controllable — which means `SpawnedProcess` needs a protocol seam.

## What's needed

1. Introduce a `SpawnedProcessHandle` (or similarly named) protocol in `CLIClient.swift` with `isRunning: Bool { get }` and `processIdentifier: Int32 { get }` (and `terminate()` if any caller needs it). `SpawnedProcess` conforms.
2. Update `CLIRecorderService.currentProcess` to return the protocol type, not the concrete class.
3. Add `FakeSpawnedProcess` in `CLIRecorderServiceTests.swift` (or a shared test utility).
4. Add `testCmdQTimeoutSendsSIGKILLToRunningCLIProcess` and `testCmdQTimeoutSkipsSIGKILLWhenProcessAlreadyExited` to `RecorderControllerTests.swift`. The first test injects a fake CLI service whose `currentProcess.isRunning` returns true and asserts `kill` was invoked (or — to avoid mocking `kill` — assert a `terminate()` call on the fake). The second injects `isRunning=false` and asserts no termination.

Both `kill(pid, SIGKILL)` and `Process.terminate()` are awkward to assert directly. Cleanest is to route the SIGKILL call through an injected closure (`killProcess: (Int32) -> Void`) on `StopPolicyCoordinator` already accepts in the plan — the orchestrator passes a closure that calls `kill(pid, SIGKILL)`. The fake can substitute a closure that records invocations.

## Why this wasn't auto-fixed

This needs the `SpawnedProcessHandle` protocol seam first, which changes the `CLIClient` / `CLIRecorderService` surface in ways that may want PR-author input.

## Source

Code review of PR #185 (finding #6, severity P1, anchor 85). Run artifact: `/tmp/compound-engineering/ce-code-review/20260520-7f1f8961/`.
