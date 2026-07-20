---
title: "macOS: dev-build Screencap.app copies masquerade as the shipped install and tangle the helper/TCC"
slug: macos-shipped-vs-dev-build-confusion
date: 2026-06-30
category: workflow-issues
problem_type: workflow_issue
component: macos_app_shell
platform: macos
severity: high
applies_when:
  - "Testing the macOS app after also building it locally (Xcode Cmd+R or script/build_and_run.sh)"
  - "Spotlight/Launchpad shows more than one \"Screencap\" and you can't tell which you're launching"
  - "The setup walkthrough shows \"macOS rejected the helper signature\" and/or the orange \"Ad-hoc dev build\" banner"
  - "daemon.info reports a permission denied while System Settings looks granted, on what you think is the shipped app"
root_cause: ambiguous_build_identity
---

## Problem

You install the shipped, notarized `Screencap.app` from the DMG — but the setup
walkthrough still fails with **"macOS rejected the helper signature"** and shows
the orange **"Ad-hoc dev build"** banner, or Accessibility reads denied while
System Settings looks granted. It looks like the *release* is broken. It usually
isn't. The machine has **more than one `Screencap.app`**, and you launched a
**dev build** instead of the shipped one.

A whole multi-hour debugging detour came from exactly this: the error screenshot
was from an ad-hoc dev build, not the notarized release — but every symptom
*looked* like a release-signing bug.

## Why it happens

There are **two completely different `Screencap.app` identities** on a dev machine:

| | Shipped app | Dev build |
|---|---|---|
| Where | `/Applications/Screencap.app` (dragged from the DMG) | `~/Library/Developer/Xcode/DerivedData/.../Screencap.app`, `macos/.build/.../Screencap.app` |
| Built by | `script/sign_app.sh` + `script/notarize_app.sh` | Xcode Cmd+R, `script/build_and_run.sh` |
| Signature | **Developer ID + notarized** (`TeamIdentifier=2A8S6MV8DZ`) | **ad-hoc** (`Signature=adhoc, TeamIdentifier=not set`), or local Apple-Development |
| Helper register | works (`SMAppService.register()` succeeds) | **fails** → `kSMErrorInvalidSignature` → "macOS rejected the helper signature." (ad-hoc can't register a LaunchAgent) |

The trap: **Spotlight and Launchpad index every copy under the same name
"Screencap"** — there is no visible way to tell which one you're launching. Each
Xcode build and each `build_and_run.sh` run drops another copy, so they
accumulate. Launch an ad-hoc dev copy and you get the helper-signature failure +
the ad-hoc banner, plus tangled TCC: the dev daemon and shipped daemon share the
LaunchAgent label `com.screencap.daemon` and the bundle id `com.screencap.macos`,
and the launchd session gets polluted with dev env vars
(`SCREENCAP_DAEMON_USE_DEV_SOURCE=1`, `SCREENCAP_CLI_PATH`, `SCREENCAP_DEV_REPO_ROOT`).

This is the same root family as
[`macos-ad-hoc-signing-tcc-rebuild-treadmill.md`](../build-errors/macos-ad-hoc-signing-tcc-rebuild-treadmill.md)
(ad-hoc signing ↔ TCC), seen from the workflow angle.

## The 5-second "which am I running?" check

Before debugging *anything* macOS-app-related, confirm which build is live:

```bash
# 1. Which copies even exist?
mdfind "kMDItemKind == 'Application'" | grep -i Screencap.app

# 2. Is the one you launched the signed install, or an ad-hoc dev build?
codesign -dvv /Applications/Screencap.app 2>&1 | grep -E "Signature|TeamIdentifier"
#   shipped → TeamIdentifier=2A8S6MV8DZ          (Developer ID, notarized)
#   dev     → Signature=adhoc / TeamIdentifier=not set

# 3. Which binary is the running daemon? (dev path ⇒ you're on a dev build)
ps aux | grep "[s]creencap serve"
```

If `TeamIdentifier` is present and `spctl --assess --type exec /Applications/Screencap.app`
says `source=Notarized Developer ID`, you're on the shipped app and any helper
failure is a *real* bug worth investigating. If it's `adhoc`, stop — you're
testing a dev build and the failure is expected.

## Guidance: decide which you're testing, and clean up dev state afterward

**Be explicit about the mode before you start:**

- **Testing the shipped app?** Install from the notarized DMG by **dragging to
  `/Applications`** (the drag clears quarantine and avoids App Translocation),
  launch it from `/Applications` (not from the DMG or `~/Downloads`), and make
  sure no dev copy is what Spotlight hands you.
- **Testing a dev build?** Fine — but it will leave ad-hoc app copies, a
  dev-tainted daemon registration, dev launchd env vars, and dev TCC rows behind.
  **Clear them when you're done**, or the next "shipped app" test inherits the mess.

**Teardown after dev testing — one command:**

```bash
script/clean_dev_macos_state.sh            # quits app, removes dev daemon reg +
                                           # socket, clears dev env, trashes stray
                                           # dev Screencap.app copies
script/clean_dev_macos_state.sh --dry-run  # preview first
script/clean_dev_macos_state.sh --reset-tcc # also reset TCC (drops the SHIPPED
                                           # app's grants too — deliberate)
```

It **never** touches `/Applications/Screencap.app`, moves strays to the Trash
(reversible), and prints the surviving shipped app's signature so you can confirm
it's intact.

## Why this matters

The shipped/dev ambiguity turns an expected dev-build limitation ("ad-hoc can't
register a helper") into a phantom "the release is broken" bug. Drawing the line
explicitly — and tearing down dev state after testing — keeps a dev machine
honest, so a real release regression isn't buried under self-inflicted dev
clutter.

## Related

- [`docs/solutions/build-errors/macos-ad-hoc-signing-tcc-rebuild-treadmill.md`](../build-errors/macos-ad-hoc-signing-tcc-rebuild-treadmill.md) — the ad-hoc ↔ TCC root cause + the two-row Accessibility trap.
- `script/clean_dev_macos_state.sh` — the teardown this doc prescribes.
- `script/sign_app.sh` / `script/notarize_app.sh` — how the *shipped* app is actually produced (Developer ID sign + notarize).
- `macos/README.md` — "One-time setup: signing identity for dev builds" and "TCC permissions on dev builds".
