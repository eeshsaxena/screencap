---
title: "Ad-hoc-signed dev builds appear as a new app to TCC on every rebuild — orphaning prior grants"
slug: macos-ad-hoc-signing-tcc-rebuild-treadmill
date: 2026-05-01
updated: 2026-06-15
category: build-errors
severity: medium
problem_type: dev-environment-friction
modules:
  - macos/ScreenCap/ScreenCap.entitlements
  - macos/project.yml
  - macos/ScreenCap/Scripts/embed-cli.sh
  - macos/ScreenCap/Scripts/screencap-cli.entitlements
tags:
  - macos
  - tcc
  - signing
  - xcode
  - dev-environment
  - macos-app-shell
  - daemon
  - pyinstaller
symptoms:
  - "Granted Screen Recording / Accessibility / Input Monitoring to ScreenCap once; rebuilt the app; permissions all show red again"
  - "Quit & Relaunch button doesn't help — the new process is treated as a different app entirely"
  - "Multiple ScreenCap entries appear in Privacy & Security after several rebuilds"
  - "`tccutil reset` is the only way to recover a clean grant flow"
  - "App is team-signed but TCC grants STILL churn on rebuild — because the nested daemon binary is ad-hoc"
  - "Two separate `screencap` rows appear in a Privacy pane (one per rebuild's cdhash)"
  - "Daemon reports denied via `/v0/daemon.info` (and the walkthrough \"ScreenCap helper\" rows) while System Settings shows the same permission granted — the app and daemon are different TCC subjects"
  - "`tccutil reset <Service> screencap` fails with OSStatus -10814; the Screen Recording entry reads \"ScreenCap\" but Accessibility / Input Monitoring read lowercase `screencap`"
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

## Update (2026-06-08): signing the *app* isn't enough — the embedded daemon binary is the TCC subject

A later spike sharpened the root cause. Screen capture is performed not by the
app but by the **nested PyInstaller `screencap` binary** the daemon runs
(`Contents/Resources/screencap/screencap`). *That binary*, not the app, is the
TCC subject for the daemon's Screen Recording / Accessibility / Input Monitoring
grants — and it stays **ad-hoc even when the app is team-signed**, because:

- `embed-cli.sh` (the "Embed screencap CLI" build phase) `ditto`s the PyInstaller
  output into `Contents/Resources/` but never signs it; and
- Xcode's automatic signing signs the app wrapper + main executable but does
  **not recurse into `Contents/Resources/`** (no `--deep`).

So `codesign -dvv …/Contents/Resources/screencap/screencap` shows
`Signature=adhoc, TeamIdentifier=not set` while the app shows your team. The
daemon's grants churn on every rebuild even though the app's own grants persist.
Tell-tale: **two separate `screencap` rows** appear in a pane after two rebuilds —
one per cdhash.

### Fix (implemented)

`embed-cli.sh` now re-signs the embedded bundle after `ditto`, inner-to-outer
(every nested `.dylib`/`.so`, then the exe), with the resolved team identity
(`EXPANDED_CODE_SIGN_IDENTITY` — confirmed available in the preBuild phase) +
hardened runtime + `Scripts/screencap-cli.entitlements` (allow-jit /
allow-unsigned-executable-memory / disable-library-validation, which
CPython + PyInstaller need under hardened runtime). It skips signing and leaves
the bundle ad-hoc when no team identity is set, preserving the CI fallback.

Validated on macOS 26.5.1 (full `xcodebuild`): the nested binary becomes
`TeamIdentifier=2A8S6MV8DZ` with hardened runtime, and its **Designated
Requirement is identity-anchored, not cdhash-anchored**:

```
designated => identifier screencap and anchor apple generic
              and certificate leaf[subject.CN] = "Apple Development: …"
```

A rebuild with the same identifier + same cert satisfies that same requirement,
so TCC keeps the grant — the per-build treadmill stops. An **Apple Development**
cert is enough to end the build-to-build churn; because the DR pins the leaf cert
CN, a cert rotation (≈yearly) or a move to **Developer ID** re-grants once.
Developer ID remains the stable, distribution-grade anchor (team-anchored DR, no
per-build or per-cert churn). Full investigation, including that TCC attributes a
request to the *responsible process* (so the daemon must issue its own
registration request, not the app):
`docs/research/2026-06-05-daemon-tcc-registration-spike.md`.

## Diagnosing it at runtime (2026-06-15): daemon says "denied" while System Settings shows "granted"

The most confusing presentation of this problem: the first-run walkthrough's
"ScreenCap helper" rows (and `/v0/daemon.info`) report Screen Recording /
Accessibility / Input Monitoring as **denied**, yet System Settings shows them
**granted** for ScreenCap, and recording is blocked. It looks like a probe bug.
It isn't — it's the identity split above, plus three macOS-UI quirks. **Don't
debug `permission_probe.py` / `daemon.info` — diagnose the TCC identity.**

**1. Read ground truth from the daemon, not System Settings:**
```bash
curl -s --unix-socket ~/.screencap/run/api.sock http://localhost/v0/daemon.info
# → "permissions":{"screen_recording":"denied","accessibility":"denied","input_monitoring":"denied"}
```
The probe spawns a fresh `screencap _permission-probe` subprocess, so this is the
*live* state for the daemon's identity (not a stale in-process cache).

**2. Inspect signing of BOTH the app AND the embedded CLI** (the daemon's real subject):
```bash
ps aux | grep "screencap serve"        # PPID 1 ⇒ launchd-parented background daemon
codesign -dv --verbose=2 <App>.app
codesign -dv --verbose=2 <App>.app/Contents/Resources/screencap/screencap
```
`Signature=adhoc` / `TeamIdentifier=not set` on the embedded CLI ⇒ the treadmill
(grants orphaned every rebuild). A team identifier ⇒ stable; the denial is just an
ungranted-or-wrong-entry, fixed below.

**3. Account for three quirks that make a correct grant *look* broken:**
- **Per-pane name divergence.** Screen Recording attributes to the containing
  bundle, so its entry shows as **"ScreenCap"** (capitalized). Accessibility and
  Input Monitoring attribute to the bare tool, so their entries show as lowercase
  **`screencap`**. You enable differently-named rows in different panes — "it
  never shows anything" is usually looking for the wrong name in the wrong pane.
- **Default OFF + stale pane.** A freshly-registered entry is **default OFF**, and
  the Screen Recording pane does **not** live-refresh. Quit System Settings
  (`Cmd+Q`) and reopen to see and toggle the new entry.
- **Input Monitoring doesn't self-register under launchd.** `IOHIDRequestAccess`
  alone registers no row for the launchd daemon (`docs/research/2026-06-05-daemon-tcc-registration-spike.md`);
  add it via the **`+`** button. Input Monitoring is advisory — it does **not**
  block recording, so don't chase it first.

After granting, the walkthrough rows flip green within ~5s (fresh probe). If a
just-toggled grant doesn't take, restart the daemon so it re-reads live TCC:
```bash
launchctl kickstart -k gui/$(id -u)/com.screencap.daemon
```

## Recovery

```bash
# Resets the APP bundle's grants only — NOT the daemon's:
tccutil reset All com.screencap.macos
```

**`tccutil` cannot reset the daemon's grants.** The daemon's subject is the bare
`screencap` tool, which has **no bundle id**, so `tccutil reset <Service> screencap`
fails with `OSStatus -10814 "No such bundle identifier"`. Clean up the daemon's
orphaned/duplicate `screencap` (and any `screencapspike`) rows **manually** with
the **"−"** button in each pane (Screen Recording, Accessibility, Input Monitoring).
Then re-grant via the walkthrough (its **Grant** buttons make the daemon register
its own identity per pane) and toggle the new rows on per the runtime steps above.
Do **not** use `tccutil reset <Service>` with no client — that wipes the service
for *every* app on the machine.

If you're going to do a smoke-test cycle, **don't rebuild between attempts**. Grant once, click Quit & Relaunch (which handles the in-process cache), test what you need to test, only then rebuild.

## Long-term fix

> **Update (2026-06-08):** the per-build treadmill is now fixed for dev — `embed-cli.sh`
> team-signs the embedded daemon binary (see the 2026-06-08 section above), so a stable
> Apple Development cert keeps grants across rebuilds. This section is the
> distribution-grade story, which a Developer ID identity completes.

Sign with a stable Developer ID Application identity. The release pipeline (Unit 1 / Unit 22 of the v1 plan) handles this. Once we sign with a real team identifier, TCC tracks `(bundle id, team id)` across rebuilds and the treadmill stops.

For dev-time stability before then, you could:

- Generate a self-signed Developer ID in Keychain Access, then sign Debug builds with `codesign -s "ScreenCap-Dev" --entitlements ...` from a post-build script. Heavy lift for marginal benefit.
- Add the .app to the Privacy & Security `+` button manually instead of via the request API — same orphaning problem on rebuild, but skips the prompt churn.
- Move the .app to `/Applications/` once and only build there — doesn't help; the signature still changes.

The pragmatic answer until a real Developer ID lands: accept the `tccutil reset` ritual, batch your smoke-tests, and rebuild as rarely as possible.

## Related

- `macos/README.md` — "TCC permissions on dev builds" (the user-facing version of this lesson; now documents both TCC subjects + the per-service reset).
- `docs/solutions/runtime-errors/macos-tcc-per-process-cache-quit-and-relaunch.md` — the in-process cache problem (separate concern, same domain: that one is "granted but a *running* process can't see it"; this one is "denied because the *wrong subject* was granted").
- `docs/solutions/build-errors/env-export-prefix-silently-disables-team-signing.md` — a `.env` `export ` prefix silently dropping `DEVELOPMENT_TEAM`, which lands you back on the ad-hoc treadmill.
- `docs/research/2026-06-05-daemon-tcc-registration-spike.md` — the source-of-truth spike (daemon identity, per-pane registration, Input-Monitoring-under-launchd, responsible-process attribution).
- `docs/solutions/design-patterns/fallback-path-recovery-and-exit-code-signaling-2026-06-15.md` — related permission-onboarding UX from the same work (PR #232, SCR-142).
