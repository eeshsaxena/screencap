---
title: "Promote DaemonSessionService to a protocol seam (match DaemonInstallController pattern)"
status: completed
priority: medium
created: 2026-05-20
completed: 2026-05-20
resolved_in: f8ccda35
source: code-review PR #185 (finding #18)
related_plans:
  - docs/plans/2026-05-19-001-refactor-split-recorder-controller-plan.md
related_review_run: /tmp/compound-engineering/ce-code-review/20260520-7f1f8961/
---

# Promote DaemonSessionService to a protocol seam

## Problem

`DaemonSessionService` is the most complex extracted collaborator (~214 lines including the reconnect loop, cursor handling, snapshot replay, typed-failure translation, and `launchctl kickstart`). It is the only new collaborator without a protocol seam — every other one (`PermissionWatchdog`, `RecorderAlertPresenter`, `CLIRecorderService`) follows the `@MainActor protocol` + `Live<Name>` pattern established by `DaemonInstallController.swift` (`DaemonRegistrationService`, `DaemonProbe`).

The injection in `RecorderController.init` takes the concrete `DaemonSessionService` type. Tests for `RecorderController` cannot substitute a fake daemon service — they must either spin up `UnixHTTPTestServer` (which is what `RecorderControllerDaemonTests` does today) or skip the daemon path entirely. The most valuable extracted logic (10-failure budget, cursor recovery, schema-mismatch translation) consequently has no fast, deterministic test surface; coverage rests on the end-to-end socket tests, which the PR body notes have a pre-existing ~1-in-3 socket-cleanup flake.

## What's needed

1. Declare `@MainActor protocol DaemonSessionService { ... }` with the public surface currently exposed by the concrete class (probe / snapshot / start / stop / event stream subscribe / cancel / reload).
2. Rename the concrete class to `LiveDaemonSessionService: DaemonSessionService`.
3. Change `RecorderController.init` to take `DaemonSessionService` (the protocol type) with `LiveDaemonSessionService()` as the live default.
4. Add `FakeDaemonSessionService` in `DaemonSessionServiceTests.swift` (or a shared test utility) that lets tests drive event-stream callbacks deterministically.
5. Migrate the consumeEventStream reconnect-loop + cursor_unknown + 10-failure-budget scenarios from `RecorderControllerDaemonTests` (where they live as `UnixHTTPTestServer` integration tests) into focused tests against the fake.

## Why this wasn't auto-fixed

Touches `RecorderController.init` surface and any test that constructs the controller. `RecorderControllerTests.swift` and `RecorderControllerDaemonTests.swift` are protected (R3 promise: existing tests pass unchanged), so the init change needs default-argument shimming to preserve source compatibility. Worth pairing with the StopPolicyCoordinator init-injection follow-up (#19) so the orchestrator's init signature is touched once, not twice.

## Source

Code review of PR #185 (finding #18, severity P2, anchor 75). Run artifact: `/tmp/compound-engineering/ce-code-review/20260520-7f1f8961/`.
