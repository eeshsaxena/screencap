---
title: "Distinguish 'another daemon already bound' from generic startup failure"
status: open
priority: high
created: 2026-05-08
related_plans:
  - docs/plans/2026-05-08-001-feat-daemon-architecture-phase-1-plan.md
related_review_run: /tmp/compound-engineering/ce-code-review/20260508-210223-e214ede7/
---

# Distinguish 'another daemon already bound' from generic startup failure

## Problem

When a same-EUID rogue process binds `~/.screencap/run/api.sock` first (or a stuck previous daemon never released it), our daemon exits with code 1 — indistinguishable from any other startup failure. Under launchctl `KeepAlive {Crashed: true, SuccessfulExit: false}`, exit-code-1 retries get throttled into a slow restart loop without any user-visible signal that the cause is "the socket is owned by someone else."

The original review finding cited `launchagent.py:87` but the actual rogue-bind exit lives at [src/screencap/daemon/server.py:125-127](../../src/screencap/daemon/server.py) (the `DaemonAlreadyRunning` raise). Evidence shifted; treat that line as the canonical site for this fix.

## What's needed

1. Use a dedicated exit code (75 = `EX_TEMPFAIL`) for the rogue-bind / already-running case. launchctl will retry but the operator can `launchctl print gui/$UID/com.screencap.daemon` and see the distinct LastExitStatus.
2. Capture rogue PID via `lsof -U <socket-path>` (or equivalent) and log it once at the failed-bind moment. Useful diagnostic.
3. Update `screencap serve --install`'s `InstallResult.state` to surface this distinct case explicitly (e.g. `install_failed_already_running` vs. `install_failed_daemon_did_not_start`).

## Acceptance

- Manual verification: pre-bind the socket from a separate process, run `screencap serve` directly, observe exit code 75 and a log line identifying the rogue PID.
- `screencap serve --install --json` (when that lands — see related ticket on `serve` JSON output) returns the distinct state string.

## References

- Tier-3 ce-code-review run artifact: `/tmp/compound-engineering/ce-code-review/20260508-210223-e214ede7/`
- Reviewer: adversarial (confidence 75, P1)
- Related: agent-native gap on `screencap serve --install/--uninstall/--status` lacking `--json` output.
