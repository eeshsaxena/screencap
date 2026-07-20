---
title: Daemon version-mismatch reinstall can surface a false failure (no convergence budget)
status: open
priority: high
created: 2026-06-15
source: code-review PR #231
related_pr: https://github.com/proteus-computer-use/screencap/pull/231
related_linear: SCR-121
related_review_run: /tmp/compound-engineering/ce-code-review/20260615-101441-f760d0b5/
finding: "#1"
severity: P1
location: macos/Screencap/Controllers/DaemonInstallController.swift:185
reviewers: adversarial, reliability
confidence: 75
---

# Daemon version-mismatch reinstall can surface a false failure (no convergence budget)

## Problem

When a stale/older Screencap daemon is squatting `~/.screencap/run/api.sock`, `DaemonInstallController` reinstalls once then re-polls — but `pollDaemon` returns `.versionMismatch` on the **first** probe that sees the wrong version (only the `.timedOut` branch polls to the deadline), and `refresh()` re-registers the agent **without stopping the running daemon**. If the old daemon is still answering when the single second-pass probe fires — and a freshly registered daemon cannot bind while it is (`socket.py` `DaemonAlreadyRunning`) — the user hits a false `.daemonVersionMismatch` on the exact path SCR-121 exists to fix.

`build_and_run.sh` added an explicit `launchctl bootout` for precisely this reason; the shipped app's install path has no equivalent, so the developer path is hardened against the squatting daemon while the user path is not.

## Suggested fix

Give the post-refresh poll a **convergence budget**: keep probing until the deadline, tracking the last-seen mismatch version, and only conclude `.versionMismatch` if the wrong version persists at the deadline (mirror the existing `.timedOut` budget). First **verify whether `SMAppService.unregister()` already stops the running daemon synchronously**; if not, also `bootout` the stale daemon before re-polling, mirroring `reconcile_daemon_version` in `build_and_run.sh`.

## Why this wasn't auto-fixed in review

The poll-to-deadline change breaks the `FakeDaemonProbe` test model: it currently returns one value per `pollDaemon` **call**, and the existing tests assert `probe.callCount == 2` and `refreshedPlistNames` based on that 1:1 mapping. A multi-probe-per-call change invalidates those assertions, so it requires a test-fixture redesign plus an Xcode build to verify — beyond a safe in-review autofix.

## Next steps (PR author)

1. Confirm `SMAppService.unregister()` semantics (does it synchronously terminate the running KeepAlive daemon before `register()`?).
2. Implement the convergence budget in `pollDaemon`; add an app-side `bootout` if unregister is not synchronous.
3. Rework `FakeDaemonProbe` so it can model a non-instantaneous swap (e.g. a scripted per-poll sequence) and add a test with a sequence like `["0.12.7", "0.12.7", "0.20.0"]`.
4. Run the macOS Xcode test target to confirm.

## Evidence

- `macos/Screencap/Controllers/DaemonInstallController.swift:185` (versionMismatch case) and `:277` (pollDaemon returns on first mismatch — only `.timedOut` polls to deadline)
- `macos/ScreencapTests/DaemonInstallControllerTests.swift` `testReinstallRecoversWhenFreshDaemonReportsExpectedVersion` assumes an instantaneous swap (probe sequence `["0.12.7","0.20.0"]`)

## Linear filing details (for handoff session)

- **Team:** Screencap (`f3bbac41-0ec3-4f2e-ae0b-96fed6ff624f`)
- **Title:** Daemon version-mismatch reinstall can surface a false failure (no convergence budget)
- **Priority:** 2 (High)
- **Labels (use IDs):** `Bug` = `fc126e8e-a621-4acb-b326-05f1c047dbf4`
- **relatedTo:** `SCR-121`
- **State:** Backlog
- **Body:** use the Problem + Suggested fix + Evidence sections above.
