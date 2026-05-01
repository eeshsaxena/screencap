---
title: "macOS TCC permission state caches per-process — `Quit & Relaunch` is the workaround"
slug: macos-tcc-per-process-cache-quit-and-relaunch
date: 2026-05-01
category: runtime-errors
severity: medium
problem_type: runtime-behavior
modules:
  - macos/ScreenCap/Controllers/PermissionController.swift
  - macos/ScreenCap/Views/Privacy/FirstRunPermissionsView.swift
  - src/screencap/recorder.py
tags:
  - macos
  - tcc
  - permissions
  - launchservices
  - swiftui
  - macos-app-shell
symptoms:
  - "After granting Screen Recording (or Accessibility, Input Monitoring) in System Settings, the SwiftUI walkthrough sheet still shows red dots"
  - "Polling the same TCC API repeatedly returns the stale value"
  - "The 1Hz `PermissionController.refresh()` never updates"
  - "User believes the app or System Settings is broken; clicks Open Settings repeatedly with no effect"
root_cause: >
  `CGPreflightScreenCaptureAccess`, `AXIsProcessTrustedWithOptions`,
  `IOHIDCheckAccess`, and `AVCaptureDevice.authorizationStatus` all cache
  their answer at process launch. macOS does not propagate TCC grants to
  already-running processes — only a fresh process gets a fresh query.
  This is documented Apple behavior. The Python CLI works around it via
  `_check_permission_fresh` (subprocess per check); the SwiftUI shell
  cannot use the same trick because spawning the .app's binary directly
  via `Process` does not preserve the bundle's TCC identity (the spawned
  child is attributed to the spawning process).
---

## Problem

After the user grants a TCC permission in System Settings → Privacy & Security, the SwiftUI walkthrough sheet keeps showing the same red dot. The 1Hz polling timer keeps calling the in-process check API, but the cached "denied" value is what comes back. The user has no way to make the dot turn green except by quitting the app.

## Why it happens

TCC checks the bundle's code-signing identity against its database the *first* time the process queries (or attempts to use) the protected resource. The result is cached for the lifetime of that process. macOS doesn't have a "TCC state changed" notification that an in-process API can subscribe to.

The Python CLI sidesteps the cache by spawning a fresh `python3` subprocess for each check (`_check_permission_fresh` in `src/screencap/recorder.py`). That works because each spawned `python3` process gets its own TCC identity (Python is a separate signed binary), and that identity reads its own fresh entry from the TCC db.

The SwiftUI shell can't do the same trick: spawning the .app's own binary via `Process.run()` *does not* preserve the bundle's TCC identity. macOS attributes the spawned child to whatever process started it (here, the spawning ScreenCap.app — but with a different security context). Sub-binaries get sub-binary TCC identity, which is "no entry yet" for any TCC pane the bundle has been granted.

## Solution

Quit and relaunch the app via LaunchServices. A fresh process started through `NSWorkspace.openApplication(at:)` (or `open <bundle>` from a detached shell) gets the bundle's full TCC identity at launch, with whatever grants the user just made.

`FirstRunPermissionsView` exposes a **Quit & Relaunch** button that calls `PermissionController.relaunchApplication()`:

```swift
func relaunchApplication() {
    guard !isRelaunching else { return }
    isRelaunching = true
    let bundlePath = Bundle.main.bundlePath
    let detach = Process()
    detach.executableURL = URL(fileURLWithPath: "/bin/sh")
    detach.arguments = [
        "-c",
        "sleep 0.6 && exec /usr/bin/open -n \"$1\"",
        "screencap-relaunch",
        bundlePath,
    ]
    try? detach.run()
    NSApp.terminate(nil)
}
```

Order matters: terminate first, then `open` runs after a 0.6s sleep so the kernel has reaped this process before LaunchServices wakes the new one. Calling `openApplication(...)` first and `NSApp.terminate(nil)` from its callback can stack multiple instances on unsigned dev builds (the terminate callback gets delayed; double-clicks compound).

A `@Published var isRelaunching: Bool` flag debounces the button so a rapid double-click doesn't spawn two `open -n` jobs. PR3's Cmd+Q `.terminateLater` semantics requires also gating the button on `recorder.state.isRecording` — without that, a "Cancel" on the NSAlert leaves the detached `open` still firing 600ms later.

## Standard pattern

This is what Loom, 1Password, and most apps that wall their first-run on TCC permissions do. The button is a feature, not a workaround — users expect it.

## Related

- `docs/architecture/swiftui-shell.md` — "The in-process TCC cache" section.
- `macos/README.md` — "TCC permissions on dev builds" section (covers the dev-rebuild treadmill, see sibling solution doc).
- `src/screencap/recorder.py:_check_permission_fresh` — Python's subprocess-per-check workaround.
