---
title: "Inject StopPolicyCoordinator via RecorderController.init (match other collaborators)"
status: completed
priority: medium
created: 2026-05-20
completed: 2026-05-20
resolved_in: f8ccda35
source: code-review PR #185 (finding #19)
related_plans:
  - docs/plans/2026-05-19-001-refactor-split-recorder-controller-plan.md
related_review_run: /tmp/compound-engineering/ce-code-review/20260520-7f1f8961/
---

# Inject StopPolicyCoordinator via RecorderController.init

## Problem

`StopPolicyCoordinator` is constructed inline in `RecorderController` as:

```swift
private let stopPolicy = StopPolicyCoordinator()
```

Every other new collaborator (`stateMachine`, `daemonService`, `cliService`, `watchdog`, `alertPresenter`) is parameterized in the initializer with a live default. The plan's stated pattern (mirroring `DaemonInstallController`) is "Defaults to the live implementation in the controller's initializer; tests inject fakes." `StopPolicyCoordinator` skipped that step.

Consequence: tests that exercise `RecorderController.stop()` end-to-end can't substitute a fast-resolving coordinator. Today the `StopPolicyCoordinatorTests` reach the coordinator directly (calling `resolveFinalized`/`resolveStopped` externally), which exercises the coordinator's own logic but leaves the `RecorderController → StopPolicyCoordinator` integration path untestable with a controlled stub.

## What's needed

1. Declare `@MainActor protocol StopPolicyCoordinatorType { ... }` (rename to taste) covering the public surface: `runStop(quitting:transport:sendStopSignal:killProcess:) async`, `resolveFinalized(_:)`, `resolveStopped(_:)`, `cancelAll()`.
2. Rename the concrete class to `LiveStopPolicyCoordinator` (or leave the existing name and have the protocol named differently — match whatever convention pairs cleanly with the other Live* classes in the same folder).
3. Change `RecorderController.init` to accept the protocol type with `LiveStopPolicyCoordinator()` as the live default. The deinit `stopPolicy.cancelAll()` call landed by the PR review (finding #2) continues to work via the protocol.
4. Add `FakeStopPolicyCoordinator` in `StopPolicyCoordinatorTests.swift` for orchestrator-side tests that don't want to wait on real timeouts.

## Why this wasn't auto-fixed

Same reason as #18: changes the `RecorderController.init` surface. Should be batched with #18 so the init signature is touched once for both protocol promotions.

## Source

Code review of PR #185 (finding #19, severity P2, anchor 75). Run artifact: `/tmp/compound-engineering/ce-code-review/20260520-7f1f8961/`.
