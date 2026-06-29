# ScreenCap macOS app (SwiftUI)

Native SwiftUI shell that drives the bundled `screencap` CLI. Targets macOS 13+, bundle id `com.screencap.macos`.

## Requirements

- Xcode 15 or newer
- `brew install xcodegen`
- For the dev fallback: a working `python3` with this repo importable (either an editable install or `PYTHONPATH=src`)

## Generate the Xcode project

```bash
cd macos
DEVELOPMENT_TEAM=YOURTEAMID xcodegen generate
open ScreenCap.xcodeproj
```

The `.xcodeproj` is generated from `project.yml` and is gitignored. Re-run `xcodegen generate` after editing `project.yml`.

### One-time setup: signing identity for dev builds

`project.yml` reads `DEVELOPMENT_TEAM` from the environment so each developer signs with their own Apple ID without committing personal team IDs. Find yours with:

```bash
# Show your signing certificate:
security find-identity -v -p codesigning | grep "Apple Development" | head -1
# Output looks like:
#   1) ABCD1234EFGH5678IJKL "Apple Development: you@example.com (XYZ123)"
#
# IMPORTANT: the 10-char string in parentheses (XYZ123) is a per-certificate
# identifier, NOT your Team ID. The Team ID is the certificate's Organizational
# Unit (OU) — a different 10-char code. Read it straight from the cert:
security find-certificate -c "Apple Development" -p \
  | openssl x509 -noout -subject -nameopt sep_multiline \
  | sed -n 's/^[[:space:]]*OU=//p' | head -1
```

Then either prefix every `xcodegen generate` with `DEVELOPMENT_TEAM=...`, or persist it in your shell rc:

```bash
# ~/.zshrc or ~/.bashrc
export DEVELOPMENT_TEAM=YOURTEAMID
```

Without `DEVELOPMENT_TEAM` set, xcodegen leaves the placeholder in `project.pbxproj` and Xcode falls back to **ad-hoc signing** — fine for one-shot CLI builds, but every Cmd+R from Xcode produces a slightly different ad-hoc signature and TCC drops your Screen Recording / Accessibility / Input Monitoring grants on each rebuild. Setting `DEVELOPMENT_TEAM` keeps the signing identity stable across rebuilds so grants persist.

## Build

> **Note:** On a fresh clone, run `xcodegen generate` from `macos/` before any `xcodebuild` command — the `.xcodeproj` is gitignored and only exists locally. Install xcodegen first if you haven't: `brew install xcodegen`. See [Generate the Xcode project](#generate-the-xcode-project) above.

From Xcode: select the `ScreenCap` scheme and **Product → Build** (`Cmd+B`).

From the command line:

```bash
cd macos
xcodebuild -project ScreenCap.xcodeproj -scheme ScreenCap -configuration Debug build
```

### Run tests

The Swift test target lives at `macos/ScreenCapTests/`. To run it from the command line:

```bash
cd macos
xcodebuild test -only-testing:ScreenCapTests \
  -project ScreenCap.xcodeproj -scheme ScreenCap
```

If the build fails with errors like `cannot find type 'RecordingState' in scope`, the `.xcodeproj` is missing files that were added since the last `xcodegen generate`. Re-run `xcodegen generate` from `macos/` (after `brew install xcodegen` if needed) and try again.

## One-command dev run

From the repo root:

```bash
DEVELOPMENT_TEAM=YOURTEAMID ./script/build_and_run.sh
```

This script:

1. Regenerates `macos/ScreenCap.xcodeproj` from `macos/project.yml` when needed.
2. Rebuilds `dist/screencap/` with PyInstaller when the bundled CLI is missing or too old to expose `screencap serve`.
3. Builds the `ScreenCap` scheme into a deterministic local DerivedData path.
4. Publishes the local launch environment to `launchd`, then opens the signed `.app` bundle through LaunchServices so macOS permission prompts match the app shown in System Settings.

Useful variants:

```bash
./script/build_and_run.sh --verify
./script/build_and_run.sh --logs
./script/build_and_run.sh --telemetry
```

`DEVELOPMENT_TEAM` still matters here: if it is unset, the build uses ad-hoc signing and macOS may make you re-grant Screen Recording / Accessibility / Input Monitoring after rebuilds.

## Resolving the bundled CLI

`CLIClient.resolveBinary()` walks three options in order:

1. `SCREENCAP_CLI_PATH` env var pointing at a `screencap` executable.
2. `Contents/Resources/screencap/screencap` inside the .app bundle (populated by `Scripts/embed-cli.sh` from `dist/screencap/` if PyInstaller has been run).
3. `python3 -m screencap.cli` when `SCREENCAP_DEV_REPO_ROOT` is set. The repo's `src/` is prepended to `PYTHONPATH` automatically.

For the LaunchAgent helper, the app uses the bundled `Contents/Resources/screencap/screencap` binary by default. This keeps macOS permission ownership on the app/helper bundle instead of a shell or Python interpreter. Source-mode helper debugging is opt-in with `SCREENCAP_DAEMON_USE_DEV_SOURCE=1`.

For day-to-day SwiftUI development, use `./script/build_and_run.sh`; it refreshes the PyInstaller bundle when needed before launching the app.

## Launching with env vars (the part that bites everyone)

`open ScreenCap.app` does **not** propagate your shell environment. The .app launches via LaunchServices, which uses launchd's environment, which by default doesn't inherit your shell. So `SCREENCAP_DEV_REPO_ROOT` from `~/.zshrc` won't reach the app.

Three ways around it:

### From Xcode (recommended)

`SCREENCAP_DEV_REPO_ROOT` is **already baked into the scheme** by `project.yml` — it resolves to `$(SRCROOT)/..` for every developer, no manual setup needed. Cmd+R just works for system-Python users.

`project.yml` now bakes a common macOS dev PATH into the generated scheme:

```text
${HOME}/.pyenv/shims:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin
```

That covers the usual pyenv + Homebrew cases without any Xcode UI edits.

If your Python still lives somewhere else, either:

1. Edit the scheme PATH manually in **Product → Scheme → Edit Scheme → Run → Arguments → Environment Variables**, or
2. Use the `build_and_run.sh` path below.

⚠️ Manual Xcode UI edits still get wiped on every `xcodegen generate`, because the scheme is regenerated from `project.yml`.

### From the terminal — LaunchServices bundle launch

The dev script sets the required launchd environment, then opens the signed app bundle:

```bash
DEVELOPMENT_TEAM=YOURTEAMID ./script/build_and_run.sh
```

This is the preferred terminal path for testing TCC permissions because macOS tracks the app bundle identity shown in Privacy & Security.

### Globally (not recommended)

```bash
launchctl setenv SCREENCAP_DEV_REPO_ROOT /path/to/repo
```

This survives `open` but pollutes every other GUI app's environment until the next login.

## TCC permissions on dev builds

The .app is ad-hoc signed by Xcode (`TeamIdentifier=not set`). macOS TCC tracks unsigned apps by code signature, which changes on every rebuild. Net effect: every rebuild looks like a brand-new app to TCC, and any prior grants are orphaned.

Recovering after a rebuild:

ScreenCap has **two distinct TCC subjects**, and a rebuild can orphan grants for either:

- the **app bundle** `com.screencap.macos` — the identity used on the CLI-fallback path (daemon unreachable), and
- the embedded **`screencap` helper binary** — the daemon's identity, which is what `daemon.info` and the walkthrough's "for ScreenCap helper" rows reflect. It is a *separate* identity (a bare signed tool with no bundle id), so resetting the app does **not** reset it.

Reset both:

```bash
# App bundle (CLI-fallback path):
tccutil reset All com.screencap.macos

# Daemon/helper (the "ScreenCap helper" rows + daemon.info) — target the bare
# `screencap` tool per service. If tccutil reports no match, remove every
# `screencap` / `screencapspike` row in each pane with the "−" button instead.
tccutil reset ScreenCapture screencap
tccutil reset Accessibility screencap
tccutil reset ListenEvent  screencap

# Restart the daemon so it re-registers under the current signature:
launchctl kickstart -k "gui/$(id -u)/com.screencap.daemon"
```

Then re-grant via the walkthrough sheet at next launch — its **Grant** buttons make the daemon register the right identity before opening each pane.

To avoid the loop entirely, build with `DEVELOPMENT_TEAM` set (see [signing identity for dev builds](#one-time-setup-signing-identity-for-dev-builds)) or sign with a stable Developer ID. A stable signature gives both subjects a fixed designated requirement, so grants persist across rebuilds and the panes stop accumulating duplicate `screencap` rows. The release pipeline in `Unit 1` / `Unit 22` of the v1 plan handles Developer ID signing.

If a rebuild leaves the walkthrough's app-process indicators stale, click **Skip for now** or **Done** after the helper is installed to dismiss the sheet and keep testing the rest of the UI. Daemon-backed recording is enforced by the helper/engine at start time; CLI fallback still uses the app/CLI permission path.

### Why `Info.plist` has no Screen Recording / Accessibility / Input Monitoring keys

There is intentionally **nothing to "drop"** from `Info.plist` or `ScreenCap.entitlements` for these three services, and there never was. Unlike Camera / Microphone / Photos — which require an `NS*UsageDescription` string — macOS gates Screen Recording, Accessibility, and Input Monitoring through **TCC at request time** (`CGRequestScreenCaptureAccess`, `AXIsProcessTrustedWithOptions`, `IOHIDRequestAccess`), not through a declared plist key. So the app declaring none of them already means "the app does not pre-declare these permissions."

Phase 1c (SCR-49) completes the consolidation in *code*: `PermissionController.requestAndOpenSettings` no longer issues the app-process registration calls for the three services, so the daemon helper is the sole TCC subject for them on the recording path. The app-process **probes** (`CGPreflightScreenCaptureAccess` etc. in `refresh()`) deliberately stay — the CLI-fallback path still gates recording on them when the daemon is unreachable. Do not "fix" the absent plist keys or re-add the app-process request calls; both are correct as-is.

## Killing stuck instances

Ad-hoc dev builds occasionally end up in uninterruptible sleep or stack instances when relaunching. To clean up:

```bash
ps aux | grep "ScreenCap.app/Contents/MacOS/ScreenCap" | grep -v grep | awk '{print $2}' | xargs -I {} kill -9 {} 2>/dev/null
```

## Layout

| Path | Owner |
|---|---|
| `project.yml` | xcodegen source of truth for the project file |
| `ScreenCap/ScreenCapApp.swift` | App entry, scenes, environment objects |
| `ScreenCap/AppDelegate.swift` | Activation policy, terminate semantics |
| `ScreenCap/Controllers/CLIClient.swift` | Process spawn, stderr line streaming, JSON parsing |
| `ScreenCap/Controllers/PermissionController.swift` | Silent TCC checks, deep links, request APIs, relaunch helper |
| `ScreenCap/Controllers/RecorderController.swift` | Recording lifecycle (Unit 13 fills in) |
| `ScreenCap/State/RecordingsIndex.swift` | Cached `screencap list --json` output |
| `ScreenCap/Views/` | SwiftUI views |
| `ScreenCap/Scripts/embed-cli.sh` | Xcode build phase: `dist/screencap/` → `Contents/Resources/screencap/` |
| `ScreenCap/Info.plist` | Bundle metadata (regenerated by xcodegen) |
| `ScreenCap/ScreenCap.entitlements` | Hardened-runtime entitlements for the outer .app |

See `docs/plans/2026-04-28-001-feat-native-macos-ui-v1-plan.md` for the v1 design.
