---
title: Daemon poll timeout after a version mismatch surfaces a generic "did not respond" (untested)
status: done
priority: medium
created: 2026-06-15
source: code-review PR #231
related_pr: https://github.com/proteus-computer-use/screencap/pull/231
related_linear: SCR-121
related_review_run: /tmp/compound-engineering/ce-code-review/20260615-101441-f760d0b5/
finding: "#2"
severity: P2
location: macos/ScreenCap/Controllers/DaemonInstallController.swift:176
reviewers: testing
confidence: 75
---

# Daemon poll timeout after a version mismatch surfaces a generic "did not respond" (untested)

## Problem

After the version-mismatch reinstall, `handleRegisteredStatus` re-enters with `allowRegistrationRefresh: false`. If that second poll **times out** (the replacement daemon never reappears), the `.timedOut` branch does nothing, so the state lands on `.pollingFailed` ("Daemon did not respond…") rather than `.daemonVersionMismatch`. A user who started with a version-mismatched daemon then gets a generic "did not respond" message instead of the actionable "reinstall the bundled helper" guidance — and no test covers this branch, so the intended terminal state is unspecified.

## Suggested fix

Decide the intended outcome for "mismatch, then second poll times out" and make `handleRegisteredStatus` reflect it (likely surface `.daemonVersionMismatch`, threading the prior-mismatch context into the second pass). Add a test with `FakeDaemonProbe(versions: ["0.12.7", nil])` + `expectedDaemonVersion: "0.20.0"` asserting the chosen state.

## Why this wasn't auto-fixed in review

Intertwined with finding #1 (the convergence-budget change) and changes install state-machine behavior; needs an Xcode build to verify. Not a safe in-review autofix.

## Next steps (PR author)

1. Decide the intended terminal state for mismatch -> reinstall -> second-poll timeout.
2. Thread the "arrived via version mismatch" context into the second `handleRegisteredStatus` pass and set the chosen state in the `.timedOut` branch.
3. Add the `["0.12.7", nil]` test asserting it.
4. Coordinate with finding #1's `FakeDaemonProbe` rework; run the macOS Xcode test target.

## Evidence

- `macos/ScreenCap/Controllers/DaemonInstallController.swift:176` — second-pass `.timedOut` case is a no-op when `allowRegistrationRefresh` is false

## Resolution (SCR-136)

Terminal state chosen: **`.daemonVersionMismatch`** (coherent with the existing cached-`lastMismatch` path, which already surfaces it for the same failure differing only by one probe's timing; gives the actionable "reinstall the bundled helper" guidance). Threaded `priorMismatchVersion` through `refreshRegistrationAfterFailedPoll` → `handleRegisteredStatus`; the second-pass `.timedOut` branch now surfaces `.daemonVersionMismatch` when we arrived via a mismatch. First-pass pure-timeout path stays `.pollingFailed` unchanged. Test added: `testMismatchThenSecondPollTimeoutSurfacesMismatch` (`["0.12.7", nil]` + `expectedDaemonVersion: "0.20.0"`), confirmed failing on the pre-fix controller for the right reason, passing after. Full macOS test target green (only the known parallel-load flakes, verified passing in isolation).

## Linear filing details (for handoff session)

- **Team:** Screencap (`f3bbac41-0ec3-4f2e-ae0b-96fed6ff624f`)
- **Title:** Daemon poll timeout after a version mismatch surfaces a generic "did not respond" (untested)
- **Priority:** 3 (Medium)
- **Labels (use IDs):** `Bug` = `fc126e8e-a621-4acb-b326-05f1c047dbf4`
- **relatedTo:** `SCR-121`
- **State:** Backlog
- **Body:** use the Problem + Suggested fix + Evidence sections above.
