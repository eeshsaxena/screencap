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
security find-identity -v -p codesigning | grep "Apple Development" | head -1
# Output looks like:
#   1) ABCD1234EFGH5678IJKL "Apple Development: you@example.com (XYZ123)"
# The team ID is the 10-char string in parentheses at the END (e.g. XYZ123).
# To get just the team ID:
security find-identity -v -p codesigning | grep -oE '\([A-Z0-9]{10}\)' | head -1 | tr -d '()'
```

Then either prefix every `xcodegen generate` with `DEVELOPMENT_TEAM=...`, or persist it in your shell rc:

```bash
# ~/.zshrc or ~/.bashrc
export DEVELOPMENT_TEAM=YOURTEAMID
```

Without `DEVELOPMENT_TEAM` set, xcodegen leaves the placeholder in `project.pbxproj` and Xcode falls back to **ad-hoc signing** — fine for one-shot CLI builds, but every Cmd+R from Xcode produces a slightly different ad-hoc signature and TCC drops your Screen Recording / Accessibility / Input Monitoring grants on each rebuild. Setting `DEVELOPMENT_TEAM` keeps the signing identity stable across rebuilds so grants persist.

## Build

From Xcode: select the `ScreenCap` scheme and **Product → Build** (`Cmd+B`).

From the command line:

```bash
cd macos
xcodebuild -project ScreenCap.xcodeproj -scheme ScreenCap -configuration Debug build
```

## Resolving the bundled CLI

`CLIClient.resolveBinary()` walks three options in order:

1. `SCREENCAP_CLI_PATH` env var pointing at a `screencap` executable.
2. `Contents/Resources/screencap/screencap` inside the .app bundle (populated by `Scripts/embed-cli.sh` from `dist/screencap/` if PyInstaller has been run).
3. `python3 -m screencap.cli` when `SCREENCAP_DEV_REPO_ROOT` is set. The repo's `src/` is prepended to `PYTHONPATH` automatically.

For day-to-day SwiftUI development, option 3 is the path of least resistance. PyInstaller is needed when validating the embed pipeline or producing a signed release.

## Launching with env vars (the part that bites everyone)

`open ScreenCap.app` does **not** propagate your shell environment. The .app launches via LaunchServices, which uses launchd's environment, which by default doesn't inherit your shell. So `SCREENCAP_DEV_REPO_ROOT` from `~/.zshrc` won't reach the app.

Three ways around it:

### From Xcode (recommended)

`SCREENCAP_DEV_REPO_ROOT` is **already baked into the scheme** by `project.yml` — it resolves to `$(SRCROOT)/..` for every developer, no manual setup needed. Cmd+R just works for system-Python users.

**If your `python3` isn't in the default PATH** (pyenv, brew Python, conda), CLIClient can't find it because Xcode-launched processes inherit launchd's minimal PATH (`/usr/bin:/bin:/usr/sbin:/sbin`). Add a PATH entry to the scheme manually:

**Product → Scheme → Edit Scheme → Run → Arguments → Environment Variables → +**

| Name | Value |
|---|---|
| `PATH` | `/Users/<you>/.pyenv/shims:/usr/bin:/bin` (or wherever your `python3` lives) |

⚠️ **This entry gets wiped on every `xcodegen generate`** — the scheme file is regenerated from `project.yml` and Xcode UI changes don't survive. PATH isn't baked into `project.yml` because it's developer-specific (pyenv vs brew vs conda live in different places) and xcodegen has no way to set a default-when-unset value. If you regenerate the project frequently, prefer the "direct binary launch" path below.

### From the terminal — direct binary launch

`open` strips env vars; running the binary directly does not:

```bash
APP_BIN=$(find ~/Library/Developer/Xcode/DerivedData -path "*/Build/Products/Debug/ScreenCap.app/Contents/MacOS/ScreenCap" -type f | head -1)
SCREENCAP_DEV_REPO_ROOT="$(pwd)/.." \
PATH="$HOME/.pyenv/shims:/usr/bin:/bin" \
"$APP_BIN" >/tmp/screencap-stdout.log 2>/tmp/screencap-stderr.log &
```

Pipe paths PYTHONPATH for free via `mergedEnv()`.

### Globally (not recommended)

```bash
launchctl setenv SCREENCAP_DEV_REPO_ROOT /path/to/repo
```

This survives `open` but pollutes every other GUI app's environment until the next login.

## TCC permissions on dev builds

The .app is ad-hoc signed by Xcode (`TeamIdentifier=not set`). macOS TCC tracks unsigned apps by code signature, which changes on every rebuild. Net effect: every rebuild looks like a brand-new app to TCC, and any prior grants are orphaned.

Recovering after a rebuild:

```bash
tccutil reset All com.screencap.macos
```

Then re-grant via the walkthrough sheet at next launch.

To avoid the loop entirely, sign with a stable Developer ID. The release pipeline in `Unit 1` / `Unit 22` of the v1 plan handles this.

If a rebuild leaves you blocked behind the walkthrough's "Done" button (red dots persist due to the in-process TCC cache), click **Skip for now** to dismiss the sheet and keep testing the rest of the UI. Recording itself is still gated by the engine-side check at start time.

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
