---
title: "Stale daemon after an app update returns HTTP 500 on every DB-backed verb — the old process serves a replaced PyInstaller bundle and its lazy imports break"
date: 2026-07-02
category: integration-issues
module: macos-app-shell
problem_type: integration_issue
component: daemon
symptoms:
  - "macOS app shows \"Couldn't load recordings — ScreenCap daemon returned HTTP 500: Internal Server Error\" with an empty menu, right after installing a new release"
  - "recording.list, apps.list, content.search, transcript.search, timeline.query all 500; daemon.info, auth.whoami, session.snapshot stay 200"
  - "apps.list response body reveals \"reason\":\"ImportError\"; recording.list returns bare text \"Internal Server Error\""
root_cause: stale_process_after_bundle_swap
resolution_type: code_fix
severity: high
related_components:
  - daemon
  - pyinstaller
  - smappservice
tags:
  - macos
  - daemon
  - pyinstaller
  - lazy-imports
  - app-update
  - launchctl
  - http-500
  - scr-121
---

# Stale daemon after an app update returns HTTP 500

## Problem

After installing a new ScreenCap release, the app shows "Couldn't load recordings — ScreenCap daemon returned HTTP 500" and an empty menu. It happens on **every** update, even with an empty recordings directory.

## Symptoms

- The error banner appears immediately on launch; a Retry does nothing.
- Only DB/recordings-touching verbs 500: `recording.list`, `apps.list`, `content.search`, `transcript.search`, `timeline.query`. `daemon.info`, `auth.whoami`, `session.snapshot` return 200.
- `apps.list` body is JSON with `"reason":"ImportError"`; `recording.list` returns a bare `text/plain` "Internal Server Error" (its lazy import is outside the handler's `try`, so the exception escapes to Starlette's default 500).

## What Didn't Work

- **In-process reproduction**: `catalog.list_recordings()` and the full `recording.list` ASGI route return 200 against the same empty dir. The source is fine.
- **Version-skew theory**: the running daemon reports `0.20.0` — identical to the source and the newly-bundled daemon. Not a code-version difference.
- **On-disk data theory**: running the *bundled* daemon binary against a fresh HOME and against a copy of the real `~/.screencap` both return 200. The on-disk data is not the trigger.
- **FD leak / executor breakage / `schema.envelope` raising**: ruled out — only 91 FDs; `auth.whoami`/`session.snapshot` use `asyncio.to_thread` and `schema.envelope` too and return 200.

## Solution (PR #319)

The trigger is the **runtime state of the specific long-running daemon process**, not code or data. The daemon process (an SMAppService LoginItem) started hours before the app bundle on disk was replaced by the update. It keeps running while `/Applications/ScreenCap.app/...` is rewritten underneath it. PyInstaller resolves **deferred (lazy) imports from the on-disk archive at request time**, so once the bundle is swapped, any not-yet-loaded module raises `ImportError` → HTTP 500. Modules imported at daemon startup stay in `sys.modules` and keep working, which is why only the lazy-import verbs fail.

`launchctl kickstart -k gui/<uid>/com.screencap.daemon` restarts the daemon; against identical data all verbs then return 200 — confirming the diagnosis.

Durable fix, in `macos/ScreenCap/Controllers/DaemonInstallController.swift` (`restartStaleDaemonIfNeeded`), wired into `AppDelegate.applicationDidFinishLaunching`:

```swift
// On launch: if a reachable daemon's process start predates the installed
// daemon binary's mtime, it is running a superseded bundle — restart it.
guard let bundleMTime = bundleModifiedAt() else { return false }
guard bundleMTime <= now() else { return false }          // clock-skew guard
guard let info = await probe() else { return false }       // unreachable → no-op
let daemonStart = Date(timeIntervalSince1970: info.startedAt)  // daemon.info.started_at
guard daemonStart < bundleMTime else { return false }      // fresh daemon → no-op
if info.isRecording { return false }                       // never interrupt capture
return await restart()                                     // launchctl kickstart -k
```

The probe reads `daemon.info` (for `started_at`) and `session.snapshot` (for an active recording) because, being non-lazy-import verbs, a stale daemon still answers them.

## Why This Works

The invariant we actually want is "the daemon process is the one launched from the *current* on-disk bundle." Comparing the daemon's `started_at` against the bundle binary's mtime encodes that directly and **version-independently**. A fresh daemon always starts *after* its binary was written, so it is never flagged; only a daemon that predates the current binary is running superseded code.

The pre-existing SCR-121 gate (`pollDaemon`) missed this because it only bounces the daemon on a **version-string mismatch** — but the daemon/CLI version (`version("screencap")`) is a separate scheme from the app `CFBundleShortVersionString`, and releases bump the app without bumping the daemon version. Same version string, replaced bytes → the gate adopted the stale process as healthy.

## Prevention

- **The staleness restart runs on every launch**, so any future app update self-heals on next relaunch (`DaemonInstallControllerTests` covers stale/fresh/mid-recording/unreachable/undeterminable-mtime/future-mtime/boundary).
- **Don't rely on the daemon version string to detect a stale daemon.** The app and daemon versions are independent schemes. Any "is the running helper current?" check must key on something the update actually changes (process-start-vs-bundle-mtime here), not the semver.
- **Deferred imports are a liability for the long-running daemon**, not just a `--help`-speed win (see the CLAUDE.md "Heavy imports are deferred inside CLI command bodies" convention). Deferred defense-in-depth (not in PR #319): eager-import the request-path modules in the daemon `lifespan` so a stale process degrades to stale-but-valid responses instead of request-time `ImportError` 500s.
- **Diagnosability gap**: the daemon's stderr goes to `/dev/null` (fds 0/1/2), so the `ImportError` traceback was invisible via unified logging and files. A small rotating daemon log would have surfaced the root cause in minutes.

## Diagnosis Method (reusable)

1. Curl the live socket per verb: `curl --unix-socket ~/.screencap/run/api.sock http://localhost/v0/<verb>`. The split (which verbs 500 vs 200) plus `apps.list`'s `reason:ImportError` body pointed straight at lazy imports.
2. Compare `ps -o lstart -p <daemon-pid>` against `stat -f %Sm` on the bundle files — daemon start (12:00) predated the bundle rewrite (14:27–14:32).
3. `launchctl kickstart -k` the daemon and re-curl: all 200 against identical data confirms "stale process," not code/data.
