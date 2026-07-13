---
name: macos-app-release
description: Build, sign, notarize, and publish a new macOS ScreenCap.app release (.dmg). Bumps the app version, builds the embedded daemon with PyInstaller, builds the app with Xcode, Developer-ID signs + notarizes + staples, packages a DMG, tags `macos-app-vX.Y.Z`, and creates a GitHub prerelease. This is the app track — distinct from `local-release` (the headless CLI/`vX.Y.Z` track). Triggers on "release the macOS app", "new app release", "build a new DMG", "ship the app to testers", or after Swift/UI or embedded-daemon changes land.
---

# macOS App Release

ScreenCap ships **two separately-versioned, independently-released artifacts**:

- **CLI / daemon** (`vX.Y.Z`) — the headless `install.sh` / `~/.screencap/bin` channel. Use the **`local-release`** skill.
- **macOS app** (`macos-app-vX.Y.Z`) — a local-only signed + notarized `.dmg`, published as a GitHub **prerelease** ("tester build"). **This skill.**

The app **embeds a daemon built from the current Python source at build time** (`macos/ScreenCap/Scripts/embed-cli.sh` bundles `dist/ScreencapDaemon.app` and stamps `Contents/Resources/screencap-cli-version`). It does **not** download a released CLI tarball, so an app release is self-contained and does **not** require a CLI release.

## Step 0: Preflight — confirm the app actually needs a release

**First, sync local `main` with remote.** The preflight below (and the Step 1 version math) diff `HEAD` and the local tag list against the last release. A stale local `main` — or a missing newly-pushed `macos-app-v*` tag — silently produces a wrong verdict ("no release warranted" when there is, or vice-versa) and wrong version numbers. Always fast-forward before deciding:

```bash
git checkout main
git fetch origin --tags --prune
git pull --ff-only origin main
```

`--ff-only` refuses to merge if local `main` has diverged (e.g. unpushed local commits). If it fails, stop and reconcile — do **not** proceed on a diverged branch. Then run the preflight:

```bash
bash scripts/what-needs-releasing.sh
```

This diffs HEAD against the last tag of each track. Proceed here only if the **macOS app** track says a release is warranted. If only the CLI track changed, use `local-release` instead. If both changed, cut the CLI release first so the app can embed a published daemon version.

## Prerequisites

1. On an **arm64 Apple Silicon** Mac (the app ships arm64).
2. A working `.venv` (arm64) with the project + PyInstaller installed. Verify: `file .venv/bin/python | grep arm64` and `.venv/bin/python -c "import PyInstaller"`.
3. `xcodegen` on PATH (`brew install xcodegen`).
4. The gitignored `.env` present (provisioned OAuth/Firebase values — see `docs/runbooks/cloud-auth-setup.md`).
5. Signing + notarization creds exported in `~/.zshrc` (the Bash shell does **not** auto-source it). Extract them per step:
   ```bash
   eval "$(grep -E '^\s*export\s+(MACOS_SIGN_IDENTITY|APPLE_NOTARY_KEY_P8|APPLE_NOTARY_KEY_ID|APPLE_NOTARY_ISSUER_ID|SCREENCAP_DAEMON_PROVISION_PROFILE)=' ~/.zshrc)"
   ```
   Confirm `test -f "$APPLE_NOTARY_KEY_P8"` and `security find-identity -v -p codesigning | grep "Developer ID Application"`. Team ID / `DEVELOPMENT_TEAM` = `2A8S6MV8DZ`.
6. **SCR-242 — the daemon's Developer ID provisioning profile.** `script/sign_app.sh` now *requires* `SCREENCAP_DAEMON_PROVISION_PROFILE` (path to the Developer ID `.provisionprofile` that authorizes the daemon's restricted `keychain-access-groups` entitlement) — without it AMFI SIGKILLs the shipped daemon at launch, so the script hard-fails. Confirm `test -f "$SCREENCAP_DAEMON_PROVISION_PROFILE"`. If it is unset or missing, create/download the profile first per `docs/runbooks/scr-242-keychain-access-group-provisioning.md`, then add its path to `~/.zshrc`.
7. On `main`, working tree clean.

## Step 1: Decide the versions

Read the current app version from `macos/project.yml` (`CFBundleShortVersionString`). Show app-relevant commits since the last app tag and pick the next version:

```bash
git log "$(git tag --list 'macos-app-v*' --sort=-v:refname | head -1)"..HEAD --oneline --no-merges
```

- **App version** (`CFBundleShortVersionString`): patch for fixes, minor for features. Bump `CFBundleVersion` (the integer build number) too.
- **Embedded daemon version** (`pyproject.toml`): **bump it whenever the embedded daemon's code changed** (any `src/screencap` change since the last app tag). This gives the app's daemon a version string distinct from the previous app build, so the SCR-121 stale-daemon gate can tell them apart on update (a same-version bundle swap is invisible to the gate — the bug behind `project_stale_daemon_after_app_update`). If only Swift/UI changed, you may leave `pyproject.toml` alone.

## Step 2: Bump versions

1. `macos/project.yml` — `CFBundleShortVersionString` + `CFBundleVersion`.
2. `macos/ScreenCap/Info.plist` — the same two keys (keep in sync with project.yml).
3. If bumping the embedded daemon: `pyproject.toml` `version`, **then refresh the editable install** so the built binary reports it:
   ```bash
   .venv/bin/pip install -e . --no-deps
   ```
   **This refresh is mandatory.** `screencap --version` (which `embed-cli.sh` stamps into the bundle) reads the frozen `.dist-info` metadata via `importlib.metadata.version("screencap")`, **not** `pyproject.toml` live. Skip the reinstall and the app embeds the OLD version string despite the bumped `pyproject.toml`. Verify:
   ```bash
   .venv/bin/python -c "from importlib.metadata import version; print(version('screencap'))"   # must print the new version
   ```

## Step 3: Inject provisioned credentials

```bash
( set -a; . ./.env; set +a; .venv/bin/python scripts/generate_provisioned.py )
```

Writes the gitignored `src/screencap/_provisioned.py` that PyInstaller bundles. Your `.env` must define all three of `SCREENCAP_OAUTH_CLIENT_ID`, `SCREENCAP_OAUTH_CLIENT_SECRET` and `SCREENCAP_FIREBASE_API_KEY` — the generator exits non-zero (writing nothing) if any is missing. The client secret is required because the token exchange for a Google Desktop OAuth client needs it even under PKCE. Without a complete injection, the Step 4 fail-closed guard blocks the release rather than shipping a binary whose every `screencap login` fails with `client_secret is missing`.

## Step 4: Build + verify the embedded daemon

```bash
rm -rf build dist
.venv/bin/python -m PyInstaller --noconfirm pyinstaller/screencap.spec
```

Then verify the built binary (all must pass):

```bash
./dist/screencap/screencap --version                              # must be the intended daemon version
SCREENCAP_RELEASE_BUILD=1 ./dist/screencap/screencap _auth-config-check   # exit 0 (real creds, not placeholders)
./dist/screencap/screencap _smoke-test                            # exit 0, all checks pass
```

If `_auth-config-check` fails, Step 3 didn't run or `.env` was unset — re-inject and rebuild. Do **not** ship a placeholder binary.

## Step 5: Build + verify the app

```bash
export DEVELOPMENT_TEAM=2A8S6MV8DZ
( cd macos && xcodegen generate )
xcodebuild -project macos/ScreenCap.xcodeproj -scheme ScreenCap -configuration Release \
  -derivedDataPath macos/.build/ReleaseDD DEVELOPMENT_TEAM="$DEVELOPMENT_TEAM" build
```

`macos/ScreenCap.xcodeproj` is xcodegen-generated and **gitignored** — never commit it. Expect `** BUILD SUCCEEDED **`. Then verify the built app carries the right versions and a properly-stamped, team-signed embedded daemon:

```bash
APP="macos/.build/ReleaseDD/Build/Products/Release/ScreenCap.app"
/usr/libexec/PlistBuddy -c "Print :CFBundleShortVersionString" "$APP/Contents/Info.plist"   # app version
cat "$APP/Contents/Resources/screencap-cli-version"; echo                                   # embedded daemon version
"$APP/Contents/Library/LoginItems/ScreencapDaemon.app/Contents/MacOS/screencap" --version   # helper launches
codesign -d -r- "$APP/Contents/Library/LoginItems/ScreencapDaemon.app" 2>&1 | grep com.screencap.daemon  # DR names the daemon
```

The `screencap-cli-version` stamp must equal the intended daemon version (this is where the Step 2 refresh pays off). The DR naming `com.screencap.daemon` is what makes TCC grants persist across rebuilds (SCR-196).

## Step 6: Sign, notarize, package the DMG

```bash
eval "$(grep -E '^\s*export\s+(MACOS_SIGN_IDENTITY|APPLE_NOTARY_KEY_P8|APPLE_NOTARY_KEY_ID|APPLE_NOTARY_ISSUER_ID|SCREENCAP_DAEMON_PROVISION_PROFILE)=' ~/.zshrc)"
APP="macos/.build/ReleaseDD/Build/Products/Release/ScreenCap.app"

# Inside-out Developer ID signing (hardened runtime). Gatekeeper 'rejected' at the
# end is EXPECTED pre-notarization. SCR-242: sign_app.sh embeds the daemon's
# Developer ID provisioning profile from SCREENCAP_DAEMON_PROVISION_PROFILE (it
# hard-fails if unset) so the restricted keychain-access-groups entitlement is
# AMFI-authorized — see docs/runbooks/scr-242-keychain-access-group-provisioning.md.
MACOS_SIGN_IDENTITY="$MACOS_SIGN_IDENTITY" \
  SCREENCAP_DAEMON_PROVISION_PROFILE="$SCREENCAP_DAEMON_PROVISION_PROFILE" \
  script/sign_app.sh "$APP"

# Notarize (uploads to Apple, minutes) + staple + build/sign/notarize/staple the DMG.
APPLE_NOTARY_KEY_P8="$APPLE_NOTARY_KEY_P8" APPLE_NOTARY_KEY_ID="$APPLE_NOTARY_KEY_ID" \
APPLE_NOTARY_ISSUER_ID="$APPLE_NOTARY_ISSUER_ID" MACOS_SIGN_IDENTITY="$MACOS_SIGN_IDENTITY" \
  script/notarize_app.sh "$APP" <APP_VERSION>
```

Success ends with `source=Notarized Developer ID` (accepted) and writes `.build/dmg/ScreenCap-<APP_VERSION>.dmg` + `.dmg.sha256`.

## Step 7: Commit, tag, push, publish

The version bump is a **direct commit to `main`** (matches the repo pattern for prior app bumps). No Claude co-authoring in the message.

```bash
git add pyproject.toml macos/project.yml macos/ScreenCap/Info.plist
git commit -m "chore(macos): bump app version to <APP_VERSION>"   # add "; bump embedded daemon to X.Y.Z" if you bumped pyproject
git tag "macos-app-v<APP_VERSION>"
git push origin main
git push origin "macos-app-v<APP_VERSION>"
```

The `macos-app-v*` tag intentionally does **not** match the `v*` trigger, so pushing it does **not** fire the CLI release CI. Then publish the GitHub prerelease with both assets:

```bash
gh release create "macos-app-v<APP_VERSION>" \
  --title "ScreenCap macOS app v<APP_VERSION> (<short descriptor>)" \
  --notes-file <notes.md> \
  --prerelease \
  .build/dmg/ScreenCap-<APP_VERSION>.dmg \
  .build/dmg/ScreenCap-<APP_VERSION>.dmg.sha256
```

Release notes should list the tester-facing fixes/changes since the last app tag, note the embedded daemon version, and include the DMG SHA-256 + install instructions (drag to Applications, open from there — not from the mounted DMG, or App Translocation breaks the app's by-path resolution of its embedded CLI).

## Release naming & organization conventions

- **App tag:** always `macos-app-vX.Y.Z` (with the `v`). The two oldest tags `macos-app-0.1.0` / `0.1.1` predate this convention — do not add more without the `v`.
- **App title:** `ScreenCap macOS app vX.Y.Z (<short descriptor>)`.
- **CLI title (for reference, set by `local-release`):** `ScreenCap CLI vX.Y.Z`.
- **Prerelease:** app builds are marked `--prerelease` while tester-only. Consequence: GitHub pins the green **Latest** badge to the newest *non-prerelease*, i.e. the CLI. Making the app the headline is a deliberate "graduate out of prerelease" decision for when it's GA — not a per-release toggle.
- **Never rename existing tags.** `install.sh` resolves CLI releases by their `vX.Y.Z` tag; renaming published tags also breaks clones.

## Error handling

- **`embed-cli.sh` warns "dist/ScreencapDaemon.app not found"** — Step 4 didn't run or was cleaned. Rebuild the daemon before the app.
- **`screencap-cli-version` shows the old version** — you skipped the `pip install -e . --no-deps` refresh in Step 2. Refresh, then rebuild the daemon (Step 4) and the app (Step 5).
- **Embedded helper DR does not name `com.screencap.daemon`** — `DEVELOPMENT_TEAM` was unset during xcodebuild, so the helper stayed ad-hoc. Re-run Step 5 with `DEVELOPMENT_TEAM` exported.
- **Notarization "Invalid" / rejected** — read the notarytool log id it prints; usually a nested binary missing hardened runtime. `script/sign_app.sh` should have covered it; re-sign and resubmit.
- **`sign_app.sh`/`notarize_app.sh` say creds unset** — the Bash shell didn't source `~/.zshrc`; run the `eval "$(grep …)"` line first.
