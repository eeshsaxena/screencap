---
title: "SMAppService reports .enabled but launchd never spawned the daemon — onboarding dead-ends at 'daemon did not respond within 30s'"
date: 2026-07-14
category: integration-issues
module: macos-app-shell
problem_type: integration_issue
component: daemon
symptoms:
  - "Onboarding 'Screencap helper' card shows \"daemon did not respond within 30s\" (red) with an 'Approve helper' button; Screen & Accessibility rows stay 'approve the helper above first'"
  - "launchctl print gui/<uid>/com.screencap.daemon shows state = not running, runs = 0, last exit code = (never exited), job state = uninitialized"
  - "~/.screencap/run/ has no api.sock; the daemon binary runs fine when executed directly and its signature verifies (valid notarized Developer ID)"
root_cause: registered_but_unspawned_daemon
resolution_type: code_fix
severity: high
related_components:
  - daemon
  - smappservice
  - launchd
  - btm
tags:
  - macos
  - daemon
  - smappservice
  - launchctl
  - kickstart
  - onboarding
  - btm
  - launchd
  - app-update
---

# SMAppService `.enabled` but launchd never spawned the daemon

## Problem

Launching the app, onboarding's helper card dead-ends at **"daemon did not respond within 30s"** and the permission rows stay blocked on "approve the helper above first." The `com.screencap.daemon` LaunchAgent is registered and BTM-approved, but launchd has **never spawned the process** — so no socket is ever bound and every `/v0/daemon.info` poll times out.

## Symptoms

- The helper card is in `.pollingFailed`, rendered as the lowercased reason string with an "Approve helper" button (`HelperInstallCard`, `PermissionSetupUI.swift`). The specific `30s` value means the failure came from the **post-refresh convergence poll** (`pollDaemon`, `convergenceTimeoutSeconds` default 30).
- `launchctl print gui/<uid>/com.screencap.daemon`: `state = not running`, `runs = 0`, `last exit code = (never exited)`, `job state = uninitialized`, `properties = ... runatload ...`. launchd holds the registered job but has never started it.
- `~/.screencap/run/` has locks and `serve.log`/`audit.log` but **no `api.sock`**.
- The daemon binary itself is fine: `.../Contents/Library/LoginItems/ScreencapDaemon.app/Contents/MacOS/screencap --version` prints normally, `codesign --verify` passes, `spctl` accepts it as notarized Developer ID.

## What Didn't Work

- **Blaming the binary / signature**: the daemon runs directly and is validly signed + notarized. Not a Gatekeeper or crash-on-launch problem — `runs = 0` means launchd never even *attempted* a spawn (a crash would increment `runs` and set a non-`never exited` exit code).
- **Waiting / retrying in the app**: onboarding's own recovery cannot heal it (see below), so Retry just re-runs the same failing sequence.
- **Assuming `.status == .enabled` means it's running**: it does not. `SMAppService.agent(...).status` reflects the **BTM approval record**, not whether launchd can actually run the current on-disk binary.

## Solution (PR #393)

Root cause: the login item's on-disk executable was **replaced in place after registration** — a dev re-embed into the installed `/Applications/Screencap.app`, or an app update (the code already notes SMAppService does not restart the LoginItem on update, see `restartStaleDaemonIfNeeded`'s comment in `DaemonInstallController.swift`). Replacing a registered login item's Mach-O leaves launchd holding the job `uninitialized`; it will not auto-respawn the modified binary, while `SMAppService.status` still returns `.enabled`.

Why onboarding could not self-heal it, in `macos/Screencap/Controllers/DaemonInstallController.swift`:

- `SMAppServiceRegistration.register()` early-returns when `service.status == .enabled` — it does **not** kickstart, so `install()` never asks launchd to start the wedged process.
- The poll-timeout path ran `refreshRegistrationAfterFailedPoll` → `refresh()` (unregister + register of the **same, still-valid bundle path**), which re-registers but does not respawn the process.
- `restartStaleDaemonIfNeeded` (the launch-time kickstart recovery) short-circuits to a no-op unless the daemon is **reachable** (`guard let info = await probe() else { return false }`) — a never-started daemon has no `daemon.info` to probe.

The `.enabled`-but-dead state fell through every recovery path. Confirmed live: a single `launchctl kickstart gui/<uid>/com.screencap.daemon` starts the daemon, it binds `api.sock`, and it answers — the binary was always fine.

The fix adds a **non-destructive `launchctl kickstart -k` + re-poll** to the first-pass poll-timeout recovery, *before* the destructive registration refresh (`handleRegisteredStatus` `.timedOut` branch → new `kickstartAndRepoll` helper, reusing the existing `liveKickstartRestart`):

```swift
case .timedOut:
    if allowRegistrationRefresh {
        // Non-destructive recovery first: respawn a registered-but-unspawned
        // daemon in place. Fires only after a poll TIMEOUT, so there is no
        // healthy daemon to disturb; falls back to refresh if it doesn't heal.
        if await kickstartAndRepoll(timeoutSeconds: timeoutSeconds,
                                    probeIntervalSeconds: probeIntervalSeconds) {
            return
        }
        await refreshRegistrationAfterFailedPoll(...)   // unchanged fallback
    } else if let priorMismatchVersion { ... }
```

`kickstartAndRepoll` returns `true` only when `pollDaemon` then reports `.running`; otherwise it returns `false` and the existing `refresh()` fallback runs (the historical repair for a genuinely moved/deleted bundle path). The injected `kickstart` dependency defaults to `liveKickstartRestart`, which **no-ops under XCTest**, so existing state-machine tests are unchanged.

## Why This Works

Two failure modes share the `DaemonInstallController` file but need different repairs:

| | Daemon reachable? | Trigger | Repair |
|---|---|---|---|
| [Stale daemon after app update](stale-daemon-after-app-update-http-500-2026-07-02.md) | Yes — serves old bundle, HTTP 500 on lazy imports | process **predates** the new bundle | `restartStaleDaemonIfNeeded` (start-time vs bundle-mtime), launch-time |
| This doc | No — never spawned, poll times out | executable **replaced in place** → launchd holds job `uninitialized` | `kickstart` in the onboarding poll-timeout recovery |

`refresh()` re-registers the same still-valid path but never respawns the wedged process; `kickstart -k` forces launchd to start (reloading the current bundle). Gating it on a poll **timeout** means it never disturbs a healthy running daemon, and the `refresh()` fallback still covers the case a kickstart can't fix (the recorded bundle path is gone). This is exactly the ordering `refresh()`'s own docstring already recommends: "Prefer non-destructive recovery (e.g. `launchctl kickstart`) when the daemon socket already exists and only its process is wedged."

## Prevention

- **Never treat `SMAppService.status == .enabled` as "the daemon is running."** It is an approval-record read, not a liveness or launchability check. Any "is the helper up?" logic must confirm with an actual socket probe (and, when down, a kickstart), not the status enum.
- **Don't modify a registered login item's executable in place.** Re-embedding/re-signing a new daemon binary into an already-installed, already-BTM-approved bundle desyncs the launchd job (this is how the reporting machine got wedged). For dev iteration use `script/build_and_run.sh` (which does its own `bootout` + `kickstart -k`); to recover a wedged install cleanly, `launchctl bootout gui/<uid>/com.screencap.daemon` then relaunch, or reinstall the release fresh.
- **Coverage**: `DaemonInstallControllerTests` adds `testEnabledButUnspawnedDaemonHealsViaKickstartWithoutRefresh` (heals via kickstart, no destructive refresh) and `testKickstartThatDoesNotHealFallsBackToRefresh` (ineffective kickstart still falls back to refresh). The default kickstart no-ops under XCTest, so the 26 pre-existing tests are unaffected.

## Diagnosis Method (reusable)

1. `launchctl print gui/<uid>/com.screencap.daemon` — `runs = 0` + `job state = uninitialized` + `state = not running` means launchd never spawned it (distinct from a crash loop, which shows `runs > 0` and a real exit code).
2. `ls ~/.screencap/run/api.sock` — absent confirms nothing is bound.
3. Run the bundled daemon binary directly with `--version` and `codesign --verify` it — if both pass, the binary is fine and the problem is purely the launchd spawn.
4. `launchctl kickstart gui/<uid>/com.screencap.daemon`, wait ~3s, re-check for `api.sock` and `state = running` — if it comes up, the root cause is the never-spawned job, not the binary.
5. Compare the login-item executable's mtime / `codesign -dv` Timestamp against the app bundle root's mtime — an executable newer than the install means it was replaced in place after registration.
