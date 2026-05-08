---
title: "Ad-hoc-signed dev builds appear as a new app to TCC on every rebuild — orphaning prior grants"
slug: macos-ad-hoc-signing-tcc-rebuild-treadmill
date: 2026-05-01
category: build-errors
severity: medium
problem_type: dev-environment-friction
modules:
  - macos/ScreenCap/ScreenCap.entitlements
  - macos/project.yml
tags:
  - macos
  - tcc
  - signing
  - xcode
  - dev-environment
  - macos-app-shell
symptoms:
  - "Granted Screen Recording / Accessibility / Input Monitoring to ScreenCap once; rebuilt the app; permissions all show red again"
  - "Quit & Relaunch button doesn't help — the new process is treated as a different app entirely"
  - "Multiple ScreenCap entries appear in Privacy & Security after several rebuilds"
  - "`tccutil reset` is the only way to recover a clean grant flow"
root_cause: >
  Xcode signs Debug builds with an ad-hoc signature (`Signature=adhoc,
  TeamIdentifier=not set`). TCC uses the code-signing identity to track
  which entries in its database belong to which app. Without a stable
  Developer ID team identifier, TCC falls back to tracking by code
  signature digest — which changes on every rebuild. Net effect: each
  rebuild is a brand-new app to TCC, and prior grants are orphaned but
  not removed (the TCC db accumulates stale entries).
---

## Problem

Workflow:

1. Build ScreenCap.app in Xcode.
2. Launch, complete the permissions walkthrough, grant Screen Recording / Accessibility / Input Monitoring in System Settings.
3. Rebuild (any source change).
4. Re-launch — the permissions walkthrough shows all four red dots again.
5. Open Privacy & Security; ScreenCap is in the list, possibly multiple times, but the toggles for the *previous* build are now orphaned. Toggling them does nothing for the current build.

The Quit & Relaunch button doesn't fix this case — the relaunched process still has the new ad-hoc signature, which TCC sees as the unknown app.

## Why it happens

```
$ codesign -dvv /path/to/ScreenCap.app
Identifier=com.screencap.macos
Format=app bundle with Mach-O thin (arm64)
CodeDirectory v=20400 size=428 flags=0x2(adhoc) hashes=3+7 location=embedded
Signature=adhoc
TeamIdentifier=not set
```

`Signature=adhoc, TeamIdentifier=not set` is the dev-build smoking gun. With a real Developer ID, TCC tracks `(CFBundleIdentifier, TeamIdentifier)` and a rebuild keeps the same TCC entries. With ad-hoc, TCC has nothing stable to anchor on except the code signature digest — which changes whenever the binary content changes (every rebuild).

This is not specific to Xcodegen, xcodebuild, or our project structure. It's how macOS hardened-runtime + TCC work. Every dev workflow that doesn't sign with a Developer ID hits this.

## Recovery

```bash
tccutil reset All com.screencap.macos
```

Wipes every TCC grant for the bundle id. Next launch shows fresh prompts; granting in Settings creates new entries that match the current build's signature.

If you're going to do a smoke-test cycle, **don't rebuild between attempts**. Grant once, click Quit & Relaunch (which handles the in-process cache), test what you need to test, only then rebuild.

## Long-term fix

Sign with a stable Developer ID Application identity. The release pipeline (Unit 1 / Unit 22 of the v1 plan) handles this. Once we sign with a real team identifier, TCC tracks `(bundle id, team id)` across rebuilds and the treadmill stops.

For dev-time stability before then, you could:

- Generate a self-signed Developer ID in Keychain Access, then sign Debug builds with `codesign -s "ScreenCap-Dev" --entitlements ...` from a post-build script. Heavy lift for marginal benefit.
- Add the .app to the Privacy & Security `+` button manually instead of via the request API — same orphaning problem on rebuild, but skips the prompt churn.
- Move the .app to `/Applications/` once and only build there — doesn't help; the signature still changes.

The pragmatic answer until a real Developer ID lands: accept the `tccutil reset` ritual, batch your smoke-tests, and rebuild as rarely as possible.

## Related

- `macos/README.md` — "TCC permissions on dev builds" (the user-facing version of this lesson).
- `docs/solutions/runtime-errors/macos-tcc-per-process-cache-quit-and-relaunch.md` — the in-process cache problem (separate concern, same domain).
