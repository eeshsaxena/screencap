---
title: "A `.env` written as `export KEY=VALUE` silently drops DEVELOPMENT_TEAM, forcing ad-hoc signing and the TCC treadmill"
slug: env-export-prefix-silently-disables-team-signing
date: 2026-06-05
category: build-errors
severity: high
problem_type: dev-environment-friction
modules:
  - script/build_and_run.sh
  - tests/test_build_and_run_env.py
  - macos/project.yml
tags:
  - macos
  - tcc
  - signing
  - dotenv
  - dev-environment
  - macos-app-shell
  - xcodebuild
symptoms:
  - "Recordings self-terminate within seconds with \"Screencap stopped recording because screen_recording was disabled in System Settings\""
  - "The Screen Recording toggles for `screencap` / `Screencap` are ON, yet every recording is 'revoked'"
  - "`codesign -dvv` on the built app shows `Signature=adhoc, TeamIdentifier=not set` even though DEVELOPMENT_TEAM is set in `.env`"
  - "build_and_run.sh prints `warning: DEVELOPMENT_TEAM is not set` despite the line being present in `.env`"
  - "The `screenshot` table in recording.db has 0 rows for the failed recordings"
root_cause: >
  build_and_run.sh's load_local_env() parses .env as literal KEY=VALUE lines
  (deliberately, to avoid `source` code-execution) by splitting on the first
  `=`. A conventional `export DEVELOPMENT_TEAM=...` line parsed as the key
  "export DEVELOPMENT_TEAM" (embedded space), failed the identifier regex
  ^[A-Za-z_][A-Za-z0-9_]*$, and was silently skipped. With DEVELOPMENT_TEAM
  never exported, xcodebuild fell back to ad-hoc signing, and macOS TCC — which
  tracks ad-hoc binaries by cdhash — orphaned the Screen Recording grant on
  every rebuild.
---

## Problem

Every recording died a few seconds after starting, with the dialog:

> Screencap stopped recording because screen_recording was disabled in System Settings.

But Screen Recording was clearly granted: System Settings → Privacy & Security → Screen & System Audio Recording showed both `screencap` and `Screencap` toggled ON. The user never disabled anything. It happened on *every* recording.

Empirical confirmation it was a real capture failure, not a UI glitch:

- `recording.db` `screenshot` table: **0 rows** across the failed recordings — the screen reader captured nothing.
- `audit.log`: 4 recordings that session, each `recording.start` → `recording.stop` within **4–15s**.
- The running daemon was the bundled ad-hoc binary `Identifier=screencap-55554944…, Signature=adhoc, TeamIdentifier=not set`.

## Why it happens

The durable fix for the [ad-hoc rebuild treadmill](macos-ad-hoc-signing-tcc-rebuild-treadmill.md) is to sign dev builds with a real team identifier so TCC tracks `(bundle-id, team-id)` across rebuilds. `macos/project.yml` is wired for exactly that:

```yaml
DEVELOPMENT_TEAM: ${DEVELOPMENT_TEAM}
CODE_SIGN_STYLE: Automatic
```

`DEVELOPMENT_TEAM` is sourced from the repo-root `.env` by `load_local_env()` in `script/build_and_run.sh`. That parser **does not** `source` the file (sourcing would evaluate backticks / `$(…)` / `;` payloads as shell). It parses literal `KEY=VALUE` lines and rejects any key that is not a plain identifier:

```bash
while IFS='=' read -r key value || [[ -n "$key" ]]; do
  ...
  key="${key#"${key%%[![:space:]]*}"}"      # ltrim
  key="${key%"${key##*[![:space:]]}"}"      # rtrim
  if [[ ! "$key" =~ ^[A-Za-z_][A-Za-z0-9_]*$ ]]; then
    continue                                 # <-- silently skipped
  fi
  ...
  export "$key=$value"
done < "$env_file"
```

The `.env` line was written the conventional shell way:

```sh
export DEVELOPMENT_TEAM=YL664A67R4
```

`IFS='='` splits that into key=`export DEVELOPMENT_TEAM` (with an embedded space) and value=`YL664A67R4`. The key contains a space, fails `^[A-Za-z_][A-Za-z0-9_]*$`, and is **silently dropped**. Every other line in the file lacked the `export ` prefix and parsed fine, so the failure was invisible.

With `DEVELOPMENT_TEAM` empty, xcodebuild signs the app ad-hoc. TCC tracks ad-hoc binaries by code-signature digest (cdhash), which changes on every rebuild — so each rebuild is a new app to TCC and the prior Screen Recording grant is orphaned. The ON toggles belonged to *earlier* builds' identities; the running build was unrecognized.

### The chain to that specific dialog (a diagnostic trap)

The grant being orphaned for the **recording worker's** identity surfaces through the SCR-76 capture-health path, not a literal "user disabled it" event:

1. The denied worker's `utils.take_screenshot()` returns `None` every tick.
2. The screen reader counts `screen.attempt` but never `screen.output` (`src/screencap/engine/recorder.py` ~1511–1531), so the health verdict is `attempt > 0 and output == 0`.
3. `_attribute_unhealthy_reader` calls in-process `Quartz.CGPreflightScreenCaptureAccess()` (~2149), gets `False`, and labels it `screen_recording`.
4. That label is the one terminal capture-health outcome → `permission_lost` (~2260) → daemon event → Swift `handlePermissionLost` → the dialog + auto-stop.

**Key insight:** a capture-health `permission_lost(screen_recording)` can mean the running binary's TCC identity is unrecognized (ad-hoc churn / orphaned grant), **not** that the user revoked anything. The detection code is correct — capture genuinely produced nothing.

## Solution

`load_local_env()` now tolerates an optional leading `export ` keyword, stripped literally so the line is still never `source`d or evaluated (PR [#221](https://github.com/proteus-computer-use/screencap/pull/221)):

```bash
key="${key%"${key##*[![:space:]]}"}"      # rtrim
# Tolerate an optional leading `export ` — the conventional way to write a
# shell-style .env. Strip the keyword literally and re-trim.
if [[ "$key" == export[[:space:]]* ]]; then
  key="${key#export}"
  key="${key#"${key%%[![:space:]]*}"}"
fi
if [[ ! "$key" =~ ^[A-Za-z_][A-Za-z0-9_]*$ ]]; then
  continue
fi
```

After the fix: set the team in `.env` (either form works), rebuild, then clear the orphaned grants and grant once:

```bash
./script/build_and_run.sh
codesign -dvv .build/ScreencapDerivedData/Build/Products/Debug/Screencap.app 2>&1 | grep Team
#   → expect TeamIdentifier=<your team>, not "not set"
tccutil reset ScreenCapture com.screencap.macos
# relaunch, grant Screen Recording once — it now survives rebuilds
```

Regression coverage: `tests/test_build_and_run_env.py` drives the real bash `load_local_env` against an `export`-prefixed `.env` and asserts the value is exported (plain and quoted lines keep working).

## How to recognize it

- **"DEVELOPMENT_TEAM is set in `.env` but the app is still ad-hoc."** Don't trust the file — confirm the parser accepts the *line format*. The tell is already emitted: `warn_if_ad_hoc_signing()` prints `warning: DEVELOPMENT_TEAM is not set` whenever the resolved team is empty.
- A `permission_lost(screen_recording)` with the System Settings toggle ON → suspect ad-hoc identity churn before suspecting an actual revocation. Check `codesign -dvv` for `TeamIdentifier=not set` and the `screenshot` table for 0 rows.

## Prevention / gotchas

- A **silently skipped** config line is worse than a rejected one: the durable mechanism existed and was configured, but inert, so nothing pointed at the real cause. Validators that drop unrecognized input should log what they dropped.
- Embedded helpers do the recording. After confirming the `.app` is team-signed, also check `Contents/Resources/screencap/screencap` — if PyInstaller's ad-hoc signature survives the app's signing pass, the worker identity can still churn and need its own re-sign in `embed-cli.sh`.

## Related

- [Ad-hoc-signed dev builds appear as a new app to TCC on every rebuild](macos-ad-hoc-signing-tcc-rebuild-treadmill.md) — the treadmill this signing was meant to stop.
- [macOS TCC permission state caches per-process](../runtime-errors/macos-tcc-per-process-cache-quit-and-relaunch.md) — the sibling in-process-cache concern in the same domain.
- [XcodeGen stale project missing new sources](xcodegen-stale-project-missing-new-sources.md) — also depends on `DEVELOPMENT_TEAM` reaching xcodegen at the right time.
- PR [proteus-computer-use/screencap#221](https://github.com/proteus-computer-use/screencap/pull/221).
