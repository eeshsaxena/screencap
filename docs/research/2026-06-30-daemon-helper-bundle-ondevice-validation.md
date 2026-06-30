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

- [ ] **Screen Recording** — entry present & toggleable, no `+` add. Row label: **___** (expect "ScreenCap Helper")
- [ ] **Accessibility** — entry present & toggleable, no `+` add. Row label: **___**
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
