---
title: "macOS Screen Recording TCC attributes a nested LoginItem helper to the host app, not the helper — so the decoy cleanup wiped the daemon's real SR row (SCR-201)"
date: 2026-07-02
category: integration-issues
module: macos
problem_type: integration_issue
tags: [tcc, screen-recording, loginitem, permissions, responsible-process, scr-201, daemon]
---

# macOS Screen Recording TCC rolls a nested LoginItem's grant up to the host app

## Problem

On macOS Tahoe (26.5.1, notarized build), the recording daemon's **Screen
Recording** row never auto-appeared in System Settings after the proactive
permission registration fired — even though `CGRequestScreenCaptureAccess()`
returned ok. The **Accessibility** row auto-appeared fine in the same run. The
user was left with no SR row to enable, so recording silently degraded (SCR-201).

## Symptoms

- Screen Recording pane shows no ScreenCap/daemon row after onboarding, but the audit log shows `screen_recording → permission.request → ok`.
- Accessibility pane shows a working `ScreencapDaemon` row in the same run — an asymmetry between the two panes.
- `daemon.info` may still report `screen_recording: granted` (stale — see the per-process TCC cache note below), masking the missing row.

## Root cause

macOS attributes a **Screen Recording** TCC request/capture to the **responsible
host application**, not to the process that calls the API — when that process is
a **LoginItem nested inside an app bundle**. Since the SCR-196 migration the
daemon ships at `ScreenCap.app/Contents/Library/LoginItems/ScreencapDaemon.app`,
so `com.screencap.macos` (the app) is its responsible ancestor. Result:

- The daemon's `CGRequestScreenCaptureAccess()` registers the SR row under
  **`com.screencap.macos`** (the app), rendered under the app's name "ScreenCap"
  — **not** `com.screencap.daemon`.
- **Accessibility does not roll up.** `AXIsProcessTrustedWithOptions` attributes
  to the calling process's own code identity, so it correctly registers
  `com.screencap.daemon` (rendered as the helper filename "ScreencapDaemon").

The bug was the interaction with the proactive decoy cleanup, which ran
`tccutil reset ScreenCapture com.screencap.macos` **immediately after**
registration — deleting the exact SR row registration had just created. Net: no
SR row survived; Accessibility (daemon-attributed, never touched by the cleanup)
looked fine. That is the reported asymmetry.

This overturns the SCR-196/200 mental model that assumed all three daemon-owned
permissions (SR, Accessibility, Input Monitoring) attribute to the daemon helper.
Only Accessibility and Input Monitoring do. The rollup was not new — the ad-hoc
signing treadmill doc already recorded that "Screen Recording attributes to the
containing bundle" (shown as "ScreenCap") while Accessibility/IM attribute to the
helper (`docs/solutions/build-errors/macos-ad-hoc-signing-tcc-rebuild-treadmill.md`,
quirk #3). That was pre-SCR-196 (bare `screencap` helper); this doc confirms the
rollup **persisted** across the migration, now with `com.screencap.daemon` /
"ScreencapDaemon" as the helper identity. The SCR-196/200 work simply lost that
knowledge: the 2026-06-08 spike concluded SR registers under the daemon identity —
**true for the pre-SCR-196 bare binary in `Contents/Resources/` with no app
ancestor, false once the daemon became a nested LoginItem** with a responsible-app
ancestor. This is exactly the "responsible-process caveat" the spike itself flagged.

## What didn't work

- **Adding a real capture touch (SCStream/CGDisplayStream), the issue's H2.**
  Refuted on-device: registration already creates a row and it appears live; the
  row just lands on the wrong identity and is then deleted. No capture touch needed.
- **Treating it as a no-live-refresh display issue (the issue's H1).** The SR
  pane *did* refresh live on this machine. The row was genuinely gone (deleted),
  not merely undisplayed.

## Solution

Stop treating the app's Screen Recording row as a decoy — it is the daemon's real
SR identity via the rollup. In `src/screencap/daemon/tcc_cleanup.py`:

```python
# BEFORE — the SR-app reset deleted the row registration just created
ALLOWED_SERVICES_FOR_APP = frozenset({SCREEN_CAPTURE_SERVICE, ACCESSIBILITY_SERVICE})
return [
    _all_reset_command(ORPHAN_BARE_IDENTITY),
    _service_reset_command(SCREEN_CAPTURE_SERVICE, APP_IDENTITY),   # ← removed
    _service_reset_command(ACCESSIBILITY_SERVICE, APP_IDENTITY),
]

# AFTER — SR-app reset is refused fail-closed; Accessibility decoy still cleared
ALLOWED_SERVICES_FOR_APP = frozenset({ACCESSIBILITY_SERVICE})
return [
    _all_reset_command(ORPHAN_BARE_IDENTITY),
    _service_reset_command(ACCESSIBILITY_SERVICE, APP_IDENTITY),
]
```

The Accessibility app-reset stays — that decoy *is* real (confirmed on-device:
the Accessibility pane shows both a real `ScreencapDaemon` row and a stray
`ScreenCap` app decoy). Onboarding copy (`PermissionController.helperSettingsEntryName`)
was also made per-pane: "ScreenCap" for Screen Recording/Microphone,
"ScreencapDaemon" for Accessibility/Input Monitoring. Fixed in
[PR #323](https://github.com/proteus-computer-use/screencap/pull/323).

## Why this works

The app's SR grant is what actually gates the daemon's screen capture (capture
rolls up the same way the request does), so keeping the app's SR row is correct —
it is not the app "leaking" into the pane, it is the daemon's real SR subject.
Removing it from the cleanup lets the row registration creates survive.

## Diagnostic techniques worth reusing (macOS TCC on-device)

- **Reset-discrimination to find which identity owns a visible row.** `tccutil
  reset <Service> <bundle-id>` for one candidate, then reopen the pane *fresh*.
  If the row disappears, that bundle id owns it. This is how the app-vs-daemon
  attribution was proven (reset `com.screencap.daemon` → row survived; reset
  `com.screencap.macos` → row removed).
- **A long-lived daemon caches its TCC state per process.** `daemon.info`
  preflight (`CGPreflightScreenCaptureAccess()`) reports the state cached at
  process start, not live TCC. To test registration/preflight faithfully,
  `launchctl kickstart -k gui/$UID/com.screencap.daemon` for a fresh process
  first, then check.
- **`CGRequestScreenCaptureAccess()` registers at most once per process.** A
  second call in the same PID is a no-op (returns the cached determination). So a
  "Retry" that re-issues the request against the same daemon process cannot
  recreate the row — a fresh daemon process is required (see SCR-211).
- **The SR pane's live-refresh is unreliable, but a freshly-opened pane always
  reflects TCC reality.** Quit System Settings (`osascript -e 'quit app "System
  Settings"'`) and reopen the pane to read ground truth.
- **TCC throttles repeated reset+request cycles for the same client.** After many
  rapid `tccutil reset` + request iterations, macOS stops re-creating the row for
  a while (anti-prompt-spam). Expected during testing; not a product path.
- **Reading `TCC.db` directly needs Full Disk Access** for the reading process —
  usually unavailable, so fall back to observing the pane.

## Prevention

- The cleanup builder now refuses `ScreenCapture com.screencap.macos` fail-closed
  (`ALLOWED_SERVICES_FOR_APP = {Accessibility}`), with a regression test
  (`test_cleanup_never_resets_app_screen_recording`) so the SR-app reset cannot be
  reintroduced silently.
- When reasoning about a helper's TCC identity, distinguish **Screen Recording**
  (rolls up to the responsible host app for a nested LoginItem) from
  **Accessibility / Input Monitoring** (stay with the calling process's own code
  identity). Do not assume one identity across all three panes.
- The now-partially-outdated conclusions live in
  `docs/research/2026-06-05-daemon-tcc-registration-spike.md` and
  `docs/research/2026-06-30-daemon-helper-bundle-ondevice-validation.md` (both
  concluded SR registers under the daemon identity — true only for the pre-SCR-196
  bare binary, false for the nested LoginItem).

## Cross-references

- [PR #323](https://github.com/proteus-computer-use/screencap/pull/323) — the fix (Closes SCR-201).
- Follow-up SCR-211 — block-with-Retry doesn't re-fire SR registration (version-gated + once-per-process).
- `docs/solutions/build-errors/daemon-tcc-identity-migration-2026-06-30.md` — the SCR-196 bare-binary → helper-bundle identity migration that introduced the responsible-app ancestor.
- `docs/solutions/build-errors/macos-ad-hoc-signing-tcc-rebuild-treadmill.md` — prior art (quirk #3): first recorded SR attributing to the containing bundle (pre-SCR-196).
- `docs/solutions/runtime-errors/macos-tcc-per-process-cache-quit-and-relaunch.md` — the per-process TCC cache that makes a long-lived daemon report stale grant state (the restart-to-re-test technique above).
- `docs/research/2026-06-05-daemon-tcc-registration-spike.md` — the U7 spike whose "responsible-process caveat" this bug realized.
