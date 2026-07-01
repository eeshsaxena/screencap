---
title: "Daemon helper-bundle on-device validation (SCR-196 U8 gate)"
type: research
status: pending-on-device
date: 2026-06-30
plan: docs/plans/2026-06-30-002-feat-daemon-helper-bundle-tcc-plan.md
---

# Daemon helper-bundle on-device validation (SCR-196 U8 gate)

This runbook is the **go/no-go gate** for SCR-196. The code (U1–U7) is built and
build-validated (Debug `xcodebuild` compiles; `pyinstaller` emits the helper
`.app`; embed/sign scripts are shellcheck-clean; `tests/daemon/test_launchagent.py`
passes). What can only be confirmed on a real Mac with a signed build and a GUI
login session is below. **PASS → SCR-196 is fixed. FAIL → take the fallback
(see end).** Record results inline.

Mirror the 2026-06-05 spike's confound-free method: a clean identity, the helper
run under launchd as a GUI-session **agent**, never shell-run (a terminal run
inherits the terminal's TCC grants and gives a false positive).

## Environment to record

| Field | Value |
|---|---|
| macOS product version / build | |
| Hardware / arch | |
| App `CFBundleShortVersionString` | |
| Signing identity (Developer ID Application) | |
| Date | |

## 0. Clean slate

```bash
# Evict any running daemon and clear its grants + the new bundle id.
launchctl bootout gui/$(id -u)/com.screencap.daemon 2>/dev/null || true
tccutil reset All com.screencap.daemon 2>/dev/null || true
tccutil reset All screencap 2>/dev/null || true   # old bare identity, if present
# Move recording state aside (do NOT delete — restore after).
mv ~/.screencap ~/.screencap.bak.$(date +%s) 2>/dev/null || true
rm -f ~/.screencap/run/api.sock 2>/dev/null || true
```

## 1. Build, sign, notarize, staple

```bash
# 1a. Build the PyInstaller helper .app (emits dist/ScreencapDaemon.app + dist/screencap).
rm -rf build dist
PYTHONPATH=src python3 -m PyInstaller --noconfirm pyinstaller/screencap.spec

# 1b. Build the macOS app in Release (runs embed-cli.sh: embeds the .app, stamps version).
#     Use the repo's normal build path (build_and_run.sh / Xcode) with DEVELOPMENT_TEAM set.

# 1c. Developer-ID sign the built app (inside-out; asserts the helper DR names com.screencap.daemon).
MACOS_SIGN_IDENTITY="Developer ID Application: <you> (2A8S6MV8DZ)" \
  script/sign_app.sh /path/to/ScreenCap.app

# 1d. Notarize + staple.
script/notarize_app.sh /path/to/ScreenCap.app
```

**Checkpoint A — helper identity is correct (the load-bearing fact):**

```bash
codesign -d -r- "/path/to/ScreenCap.app/Contents/Library/LoginItems/ScreencapDaemon.app"
# EXPECT: requirement names  identifier "com.screencap.daemon"  + the Team ID.
codesign -dvv "/path/to/ScreenCap.app/Contents/Library/LoginItems/ScreencapDaemon.app/Contents/MacOS/screencap" 2>&1 | grep -E "Identifier|TeamIdentifier"
# EXPECT: Identifier=com.screencap.daemon  (NOT bare "screencap"), TeamIdentifier set.
```
- [ ] Helper DR names `com.screencap.daemon` — **result: ___**

## 2. Install from a quarantined / translocated download (the real first-run path)

A freshly downloaded app is quarantined and Gatekeeper may **translocate** it
(run from a randomized read-only path). This shifts the nested-exec / `sys.executable`
/ `resolveBinary()` paths — exactly the A1 new-user case. Don't skip to a clean
dev-installed copy.

```bash
# Simulate a download: zip, set the quarantine attr, unzip to /Applications.
xattr -w com.apple.quarantine "0083;$(printf %x $(date +%s));Safari;" /path/to/ScreenCap.app
# (or actually download the notarized artifact). Then move to /Applications and launch.
open /Applications/ScreenCap.app
```
- [ ] App launches from the quarantined/translocated location without path errors — **result: ___**

## 3. Native TCC listing + grant (per permission)

Complete the walkthrough. For each pane, confirm a **toggleable entry exists with
no manual `+` add**, then enable it.

```bash
open "x-apple.systempreferences:com.apple.settings.PrivacySecurity.extension?Privacy_ScreenCapture"
open "x-apple.systempreferences:com.apple.settings.PrivacySecurity.extension?Privacy_Accessibility"
open "x-apple.systempreferences:com.apple.settings.PrivacySecurity.extension?Privacy_ListenEvent"
```

- [ ] **Screen Recording** — entry present & toggleable, no `+` add. Row label: **___** (SCR-200 target: "ScreenCap" — see SCR-200 §A below)
- [ ] **Accessibility** — entry present & toggleable, no `+` add. Row label: **___** (SCR-200 target: "ScreenCap")
- [ ] **Input Monitoring** — entry present & toggleable, no `+` add. Row label: **___**  ← *the historically hardest one; the spike showed `IOHIDRequestAccess` alone did not register, so this exercises the `CGEventTapCreate` touch from the bundled identity.*
- [ ] `tccutil reset ScreenCapture com.screencap.daemon` succeeds (no "No such bundle identifier") — **result: ___**

## 4. Spawn-from-`.app` + capture-time identity (the U1 gate core)

Capture runs in `multiprocessing.spawn` workers re-exec'd from `sys.executable`.
Confirm the worker — not just the daemon parent — launches from inside the `.app`
and reads the live grant.

```bash
screencap start u8-gate-test ; sleep 4
# While recording, capture the PIDs and their code identity:
DAEMON_PID=$(pgrep -f 'ScreencapDaemon.app/Contents/MacOS/screencap serve' | head -1)
WORKER_PID=$(pgrep -f 'ScreencapDaemon.app/Contents/MacOS/screencap' | grep -v "$DAEMON_PID" | head -1)
codesign -dvv /proc-or-/path "$DAEMON_PID"   # use: codesign -dvv $(ps -o comm= -p $DAEMON_PID)
codesign -dvv "$(ps -o comm= -p "$WORKER_PID")" 2>&1 | grep Identifier
screencap stop
```

- [ ] A capture worker process actually spawns (not just the daemon) — **result: ___**
- [ ] Live daemon PID `codesign` Identifier == `com.screencap.daemon` — **result: ___**
- [ ] Spawned worker PID `codesign` Identifier == `com.screencap.daemon` — **result: ___**
- [ ] The recording contains **real frames** (open it / inspect screenshots — not wallpaper/black) — **result: ___**
- [ ] Recording works **without** `launchctl kickstart` after granting (fresh worker reads live TCC) — **result: ___**

## 5. Restore

```bash
# Remove the test recording, restore prior state.
screencap rm u8-gate-test 2>/dev/null || true
rm -rf ~/.screencap && mv ~/.screencap.bak.* ~/.screencap 2>/dev/null || true
```

## Verdict

- **PASS** — all of §1 Checkpoint A, §3 (all three native + tccutil), and §4
  (worker spawns, both PIDs == com.screencap.daemon, real frames) hold. SCR-196
  is fixed; the PR can merge + release.
- **PARTIAL — Input Monitoring only** — SR + Accessibility pass but IM does not
  populate natively even via the event-tap touch. Per the plan (R4), escalate:
  either relax IM to deep-link-assisted manual add (it is advisory, not
  capture-fatal) or block release pending an IM fix. **Do not silently ship IM
  broken.**
- **FAIL — spawn/identity** — a worker fails to spawn from the `.app`, or a PID
  resolves to something other than `com.screencap.daemon`. Take the documented
  fallback in the plan's Alternatives: re-sign the bare binary with
  `--identifier com.screencap.daemon`, or switch to `SMAppService.loginItem`
  (contained pivot across U2/U3/U5). Re-run this runbook after the pivot.

---

# SCR-200 — Foolproof onboarding validation (R1–R9)

Extends the SCR-196 gate above. SCR-200 makes the daemon's rows impossible to
get wrong: pre-registered at install, named "ScreenCap", decoys removed, and
block-with-Retry on failure. These behaviors are **only verifiable on a clean
machine** — this dev machine's TCC state is polluted. Run after the SCR-196 §0
clean slate, with the **SCR-200 build** (helper `CFBundleDisplayName='ScreenCap'`).
Mark each leg PASS/FAIL; **every R1–R9 must PASS (or carry an explicit, escalated
exception) before release.** This is the release gate; do not ship on dev-machine
results.

Plan: `docs/plans/2026-06-30-003-feat-foolproof-permission-onboarding-plan.md`.

## A. Label leg (R3, U1/U2) — the row reads "ScreenCap" **per pane**

The display-name keys (`CFBundleName`/`CFBundleDisplayName='ScreenCap'`) are the
documented lever (QA1544), but a dev-machine observation suggested the `.app`
filename wins. SR and Accessibility may also derive the label differently
(treadmill doc). Read the label in **both** panes after a fresh registration:

```bash
# After install + the walkthrough has fired registration (or run on-demand Grant):
open "x-apple.systempreferences:com.apple.settings.PrivacySecurity.extension?Privacy_ScreenCapture"
open "x-apple.systempreferences:com.apple.settings.PrivacySecurity.extension?Privacy_Accessibility"
```

- [ ] **Screen Recording** row label reads exactly **"ScreenCap"** — result: **___**
- [ ] **Accessibility** row label reads exactly **"ScreenCap"** — result: **___**
- [ ] **Label lever that won** (plist keys / InfoPlist.strings / filename) — record: **___**
- **If neither pane reads "ScreenCap" via the plist keys:** this is the U1
  no-lever / filename-wins outcome. Escalate to the U2 **rename branch** (nested
  same-name `ScreenCap.app`, bundle id unchanged) OR the U1 degrade branch
  (name + icon the *actual* rendered string per pane). Do **not** ship a build
  where the walkthrough says "ScreenCap" but the row shows "ScreencapDaemon".

## B. Pre-population leg (R1, R2, U3) — a row exists before the user toggles

Open the panes **right after install**, before any on-demand Grant click:

- [ ] A **"ScreenCap" row is already present** in Screen Recording — no `+` add — result: **___**
- [ ] A **"ScreenCap" row is already present** in Accessibility — no `+` add — result: **___**
- [ ] The grant attributes to `com.screencap.daemon` (Grant, then `tccutil reset ScreenCapture com.screencap.daemon` succeeds) — result: **___**

## C. Registration-race leg (R7, U3/U6) — pane open before the row registers

The SR pane does **not** live-refresh. Open Settings *before* registration
completes, leave the pane open while the row registers:

- [ ] With the pane already open at registration time, the documented quit/reopen-Settings recovery shows the row — result: **___**
- [ ] R7's Retry handles the transient "row not yet present" without dead-ending (no `+` prompt, no silent proceed) — result: **___**

## D. Headless-install leg (R1, R6, U3) — install over SSH (no GUI session)

```bash
# From an SSH session (no GUI login session):
screencap serve --install     # runs run_proactive_setup(): cleanup + registration round-trip
```

- [ ] Either the row appears in the GUI session's pane, **or** R7's Retry recovers gracefully (no dead-end) — result: **___**
- [ ] The grant attributes to `com.screencap.daemon`, **not** the installing shell (`sshd`/`login`) — result: **___**

## E. Decoy leg (R5, R6, R8, U4) — exactly one row; decoys gone; nothing else wiped

```bash
# Seed decoys on the test machine first (orphan + a stray app row), then install:
tccutil reset All screencap 2>/dev/null || true   # (already-clean check)
```

- [ ] After install, **exactly one "ScreenCap" row** per pane (SR, Accessibility) — result: **___**
- [ ] The orphan `screencap` row and any `com.screencap.macos` SR/Accessibility rows are **gone** — result: **___**
- [ ] The daemon's own grants are **intact** (recording still captures window/keystroke attribution) — result: **___**
- [ ] The app's **Microphone** grant is **intact** (R6 cleanup never touched it) — result: **___**
- [ ] **Relaunch the app + exercise normal flows → the app's SR/Accessibility row does NOT reappear** (confirms the shipped app never recreates it — it only reads via `CGPreflight`/`AXIsProcessTrusted(prompt:false)`) — result: **___**

## F. Block-with-Retry leg (R7, U6) — suppressed registration → block, not "+"

```bash
# Deliberately suppress registration (e.g. point the app at a daemon that no-ops
# permission.request, or revoke the GUI session) so the row never appears.
```

- [ ] The walkthrough reaches **block-with-Retry** after the budget — never a "+" add, never a silent advance — result: **___**
- [ ] A **normal** install does NOT false-block before the user has had a chance to toggle (present-but-OFF ≠ absent) — result: **___**
- [ ] **Retry** re-fires registration and recovers when it succeeds — result: **___**
- [ ] `indeterminate` (couldn't verify) keeps Retry available — not a hard false-block — result: **___**

## G. Upgrade leg (R9, U2/U7) — relabel/clean **without** re-grant

```bash
# Install an OLDER-identity build, grant it, then upgrade to the SCR-200 build.
```

- [ ] Existing daemon grants **carry over** — the user is NOT forced to re-grant (R9) — result: **___**
- [ ] The `codesign -d -r-` helper DR is **byte-identical** before/after the upgrade (grants keyed on bundle-id+Team survive) — result: **___**
- [ ] Decoys cleared on upgrade (E above holds post-upgrade) — result: **___**
- [ ] **If the rename branch was taken:** does `SMAppService` re-prompt for Login-Items approval on the helper filename change? (distinct PASS/FAIL from grant-survival) — result: **___**. Decide: acceptable (one-time prompt + guiding copy) vs blocker.

## H. Nested-bundle leg (rename branch only — skip if plist-keys lever won)

```bash
HELPER="/Applications/ScreenCap.app/Contents/Library/LoginItems/ScreenCap.app"
codesign --verify --deep --strict "$HELPER"
spctl -a -vv "$HELPER"
sfltool dumpbtm | grep -i screencap
lsregister -dump | grep -i screencap   # no name shadowing; helper registers as com.screencap.daemon at its nested path
```

- [ ] `--verify --deep --strict` + `spctl` pass on the nested same-name bundle — result: **___**
- [ ] No LaunchServices name shadowing; helper registers as `com.screencap.daemon` at its nested path — result: **___**
- [ ] The committed DR golden-file CI gate matches the shipped helper DR — result: **___**

## SCR-200 Verdict

- **PASS** — every R1–R9 leg above holds (A–G; H only if the rename branch was
  taken). The onboarding is foolproof on a clean machine and an upgrade. Ship.
- **DEGRADE (R3 only)** — no lever yields "ScreenCap" in both panes: take the U1
  degrade branch (name + icon the actual rendered string per pane). R1/R2/R5/R6/
  R7/R8/R9 must still PASS. Update U5 copy to the rendered string before release.
- **FAIL** — any of pre-population (B), decoy-without-collateral (E), or
  block-with-Retry (F) fails. Do not ship; return to the plan's mitigations.
