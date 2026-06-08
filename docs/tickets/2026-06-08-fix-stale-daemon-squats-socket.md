---
title: "build_and_run / app silently use a stale or version-mismatched daemon"
status: open
priority: medium
created: 2026-06-08
related_plans:
  - docs/plans/2026-06-05-003-feat-daemon-tcc-permission-visibility-onboarding-plan.md
related_pr: https://github.com/proteus-computer-use/screencap/pull/223
---

# build_and_run / app silently use a stale or version-mismatched daemon

## Problem

Surfaced during manual QA of the daemon-TCC feature (PR #223). A months-old daemon
(`screencap 0.12.7`) from a **different** build location (the main repo's
`.build/ScreenCapDerivedData/.../ScreenCap.app`) was still registered via
SMAppService and running, squatting `~/.screencap/run/api.sock`. When
`./script/build_and_run.sh` launched the freshly built 0.20.0 app:

1. The app probed the socket, found the stale daemon **reachable**, and concluded
   "helper already installed and running" — so it **never reinstalled** its own
   bundled daemon.
2. `launchctl kickstart` only restarts whatever bundle SMAppService has the label
   pinned to (the old one), so it does not help.
3. Result: the new app drove the **old** daemon. None of the feature code (U1–U8)
   was actually running, and the user saw the *old* two-phase failure
   ("permission revoked" / can't record) against a stale TCC grant — looking like
   a permissions bug when it was a version-mismatch bug.

The only signal that something was wrong was `GET /v0/daemon.info` reporting
`daemon_version: 0.12.7` with no `permissions` block. Nothing in the app or the
tooling flagged the mismatch.

Recovery required manual intervention:
`launchctl bootout gui/$(id -u)/com.screencap.daemon`, then relaunch the freshly
built app so it re-registers its own helper.

## Why this is a ticket (not an inline fix)

It spans the dev tooling (`script/build_and_run.sh`) and the app's
`DaemonInstallController` install/health logic, and the "right" behavior has a
design choice (warn vs. auto-replace; how to compare versions across the socket).
Worth deciding deliberately.

## What's needed

Pick one (or combine):

- **Tooling (`build_and_run.sh`):** before declaring success, compare the running
  daemon's `daemon.info.daemon_version` (and/or the bundle path it was launched
  from) against the bundle just built. On mismatch, `bootout` the stale label and
  let the fresh app reinstall — or at least print a loud warning with the
  recovery command.
- **App (`DaemonInstallController`):** treat "socket reachable" as **necessary but
  not sufficient**. Compare the reachable daemon's reported version against the
  app's expected version; if they diverge, surface a "helper is out of date —
  reinstall" path instead of silently adopting the old daemon.

## Acceptance

- Running `build_and_run.sh` against a machine with a stale/older daemon results in
  the **freshly built** daemon running (verified via `daemon.info.daemon_version`),
  or a clear, actionable warning if it can't be replaced automatically.
- A version-mismatched reachable daemon no longer reads as "installed and healthy"
  to the app without at least a surfaced warning.

## Notes

- This is purely a **dev / QA experience** problem. It does not affect shipped
  builds with a single stable install. It is adjacent to — but distinct from —
  the grant-persistence treadmill (see
  `docs/tickets/2026-06-08-track-daemon-tcc-grant-persistence.md`).
