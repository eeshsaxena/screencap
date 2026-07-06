---
title: "The daemon's daemon.info TCC-grant probe is pinned to the daemon's launch, not live — a fresh subprocess does NOT read a post-launch grant"
slug: daemon-tcc-grant-probe-stale-until-daemon-restart
date: 2026-07-06
category: runtime-errors
severity: high
problem_type: runtime-behavior
modules:
  - src/screencap/daemon/permission_probe.py
  - macos/ScreenCap/Controllers/RecorderController.swift
  - macos/ScreenCap/Views/Onboarding/OnboardingPermissionsStep.swift
tags:
  - macos
  - tcc
  - permissions
  - daemon
  - screen-recording
  - responsible-process
  - onboarding
symptoms:
  - "Onboarding permission rows stay on 'required · waiting' even though the user granted the permission in System Settings"
  - "Re-toggling the System Settings switch changes nothing; only a daemon restart clears the stale state"
  - "daemon.info reports screen_recording/accessibility/input_monitoring 'denied' while the grants are actually present"
root_cause: >
  The daemon's daemon.info grant probe spawns a fresh subprocess to dodge the
  per-process TCC cache, but that child inherits the daemon's launch-time TCC
  responsibility context (Screen Recording rolls up to the responsible host app,
  cached at the daemon's launch). So a grant made AFTER the daemon started stays
  invisible to the probe until the daemon process itself restarts. The
  "fresh subprocess reads live TCC" premise holds for a permission's own calling
  identity and for the granted→denied (revocation) direction, but NOT for a
  rolled-up grant in the denied→granted direction on a long-running,
  launchd-launched daemon.
---

# The daemon's grant probe is pinned to the daemon's launch, not live

## Problem

On macOS, a permission the user grants in System Settings **after** the ScreenCap
daemon has started is reported as `denied` by the daemon's `daemon.info` grant
probe until the daemon process restarts. The onboarding UI renders that stale
`denied`, stranding the user on `required · waiting` with no in-app way out —
exactly the class of dead end that blocks a non-technical user who has done
everything correctly.

## Symptoms

- `OnboardingPermissionsStep` rows stuck on `required · waiting`
  (`daemonGrant(for:) == .denied`) despite the grant being toggled ON in the
  Screen & System Audio Recording pane.
- Re-toggling the switch in System Settings does nothing — the grant is already
  present; the daemon just can't see it.
- Querying the live daemon confirms it:
  `curl --unix-socket ~/.screencap/run/api.sock http://localhost/v0/daemon.info`
  returns `"permissions": {"screen_recording": "denied", ...}` while the app's
  own Microphone check (done in-process, app identity) reads `granted`.

## What Didn't Work

- **Assuming the "fresh subprocess reads live TCC" design already handled it.**
  `permission_probe.py` spawns `screencap _permission-probe` specifically to dodge
  the per-process TCC cache, and its docstring (plus the sibling doc
  `frozen-daemon-cache-immune-tcc-read-via-mp-spawn.md`) asserts a fresh process
  reads live state. That is false here — see the root cause.
- **Switching the probe to a `multiprocessing`-spawn read (like `_screen_perm_probe.py`).**
  Ruled out on analysis: both mechanisms re-exec the *same* daemon binary and
  neither disclaims TCC responsibility, so the spawned child inherits the same
  launch-time responsibility context. Only a fresh *daemon* process reads live.
- **Waiting / re-polling.** The onboarding grant-watch already re-polls every ~5s
  and on app re-activation; re-polling a daemon whose answer can never change is a
  no-op, which is exactly what made the "listening for permission change…" footer
  a lie.

## The decisive experiment

With the grant already in place in System Settings:

```bash
# Long-running daemon (started before the grant):
curl -s --unix-socket ~/.screencap/run/api.sock http://localhost/v0/daemon.info
#   -> "permissions": { "screen_recording": "denied", "accessibility": "denied", ... }

launchctl kickstart -k "gui/$(id -u)/com.screencap.daemon"   # fresh daemon process

curl -s --unix-socket ~/.screencap/run/api.sock http://localhost/v0/daemon.info
#   -> "permissions": { "screen_recording": "granted", "accessibility": "granted", ... }
```

All three flipped `denied → granted` with **zero change** to the actual System
Settings grants in between — proving the probe reflects the daemon's launch-time
state, not live state. `launchctl kickstart -k gui/$UID/com.screencap.daemon` is
the reusable one-liner to read ground truth from a fresh daemon.

## Solution

Don't touch the probe mechanism — get a **fresh daemon process** when the grant
state matters. The macOS app's onboarding/permission-repair grant-watch now
restarts the daemon when a required grant reads denied while a permission surface
is visible (`RecorderController.refreshDaemonGrantsDefeatingStaleness` →
`daemonService.reload()` = `launchctl kickstart -kp`), gated by the pure
`shouldRestartStaleDaemon`:

```swift
static func shouldRestartStaleDaemon(
    anyRequiredDenied: Bool,
    isRecording: Bool,
    transportIsDaemon: Bool,
    secondsSinceLastRestart: TimeInterval?,
    cooldown: TimeInterval
) -> Bool {
    guard anyRequiredDenied, !isRecording, transportIsDaemon else { return false }
    guard let secondsSinceLastRestart else { return true }
    return secondsSinceLastRestart >= cooldown   // 8s cooldown
}
```

Never during a recording (a kickstart kills capture), only on the live daemon
transport (never mid-install / CLI-fallback), rate-limited so a coincident
activation + timer tick can't double-restart. An `isDefeatingStaleness` flag makes
`start()` refuse cleanly during the ~1s kickstart window instead of dispatching to
a daemon that is mid-relaunch. Shipped in
[PR #336](https://github.com/proteus-computer-use/screencap/pull/336).

## Why This Works

The stale read is a property of the daemon *process*, so a fresh daemon process
re-establishes the responsibility association and re-evaluates the rolled-up
grant against the current TCC DB. Restarting the daemon is the only mechanism that
observes an eventually-granted permission; the probe subprocess cannot, because
disclaiming responsibility to force a live read would break the Screen Recording
rollup (attributing to `com.screencap.daemon`, which has no SR row).

## Prevention

- **When a long-running daemon must observe a *newly granted* rolled-up TCC
  permission, restart the daemon — do not add another subprocess probe.** A fresh
  subprocess reads live TCC only for a permission attributed to the *calling
  process's own identity*, and only for the granted→denied (revocation) direction.
  For a rolled-up Screen Recording grant in the denied→granted direction on a
  launchd-launched daemon, the child inherits the daemon's launch-time context and
  reads stale.
- **Distinguish the two probe directions.** Revocation detection
  (`_screen_perm_probe.py`, mid-recording) works with a fresh worker-spawned
  child; post-launch *grant* detection does not — don't assume one implies the
  other.
- **Use `launchctl kickstart -k gui/$UID/com.screencap.daemon` to read ground
  truth** when a daemon TCC read looks wrong; compare the long-running daemon's
  `daemon.info` against a freshly kickstarted one before concluding a grant is
  genuinely missing.
- Regression coverage lives in `RecorderControllerTests` (the
  `shouldRestartStaleDaemon` decision matrix, the end-to-end restart + cooldown
  tests, and the start-refuses-during-kickstart guard).

## Cross-references

- `docs/solutions/integration-issues/macos-screen-recording-tcc-host-app-rollup-2026-07-02.md`
  — the responsible-app rollup (why SR attributes to `com.screencap.macos`, not the daemon).
- `docs/solutions/runtime-errors/macos-tcc-per-process-cache-quit-and-relaunch.md`
  — the per-process TCC cache root cause and the app-side Quit & Relaunch workaround.
- `docs/solutions/runtime-errors/frozen-daemon-cache-immune-tcc-read-via-mp-spawn.md`
  — the sibling whose "fresh subprocess reads live TCC" conclusion this doc
  narrows: true for revocation detection from a freshly-spawned worker, **false**
  for post-launch grant detection through a long-running daemon's rolled-up probe.
- [PR #336](https://github.com/proteus-computer-use/screencap/pull/336) — the fix.
