---
title: "SCR-254 Risk R-B spike — daemon microphone TCC grant"
date: 2026-07-12
topic: mid-recording-mic-mute
ticket: SCR-254
relates_to: SCR-218
kind: spike
---

# Risk R-B spike — does the embedded daemon hold its own microphone TCC grant?

**Question (from the SCR-218 plan, Risk R-B / KTD5):** macOS TCC keys mic access
on the *responsible code-signing identity*. The mic control runs inside the
PyInstaller embedded daemon, a separately-signed binary. Does the daemon inherit
the app's mic grant, or does it need its own — and can we grant it? This gates
R2 (unmute a `--no-audio` recording → start capture live), because a user who
only ever recorded `--no-audio` may never have established the daemon's mic grant.

## Findings (static analysis)

1. **The daemon is a first-class, independent TCC subject.** It ships as its own
   helper `.app` (`ScreencapDaemon.app`, bundle id `com.screencap.daemon`, its
   own `Info.plist`, executable at `Contents/MacOS/screencap`) — see
   `pyinstaller/screencap.spec` (the `BUNDLE(...)` block) and
   `macos/Screencap/Scripts/embed-cli.sh` ("Info.plist is what makes macOS treat
   the daemon as a first-class TCC subject"). So macOS attributes the daemon's
   mic access to `com.screencap.daemon`, **not** to the app.
   → **The daemon needs its OWN mic grant; the app's grant does not cover it.**
   KTD5 is confirmed.

2. **`permission.request` cannot supply a mic grant today.** The verb validates
   `permission` against `permission_probe.PERMISSION_KEYS` =
   `{screen_recording, accessibility, input_monitoring}` and returns a typed
   `invalid_permission` 4xx for anything else
   (`src/screencap/daemon/app.py:830`). Microphone is not in the allowlist, and
   the registration mechanisms (`permission_register`) have no `AVCaptureDevice`
   branch. Confirmed.

3. **The daemon was not provisioned to acquire the mic.** Two gaps found:
   - Its generated `Info.plist` declared **no `NSMicrophoneUsageDescription`**
     (`pyinstaller/screencap.spec` `info_plist` had only bundle-id / display-name
     / `LSUIElement`). A process that opens the mic without a purpose string gets
     no meaningful prompt, and AVFoundation clients are terminated outright.
   - The daemon's entitlements (`macos/Screencap/Scripts/screencap-cli.entitlements`)
     deliberately omit `com.apple.security.device.audio-input` (the app's
     `Screencap.entitlements` declares it). That entitlement is a *sandbox* key,
     not required for a hardened-runtime Developer-ID binary — TCC + the usage
     string are what gate mic access here — so this is expected, not the blocker.

## Change made in this ticket

Added `NSMicrophoneUsageDescription` to the daemon bundle's `info_plist` in
`pyinstaller/screencap.spec`. This is the prerequisite that lets the daemon
prompt for / acquire the mic under its own identity. Low-risk and purely
additive (a purpose string), and arguably a latent fix for existing audio-on
recordings driven through the daemon.

## What still needs on-device verification (manual QA — needs a real mic + a signed build)

Static analysis cannot settle the *runtime* TCC behavior of a background
(`LSUIElement`) launchd agent driving a `sounddevice`/PortAudio → CoreAudio-HAL
mic open (a different path from AVFoundation's `AVCaptureDevice`). On a **clean**
machine with a **signed** daemon build carrying the new usage string, verify:

1. **Audio-on baseline:** record with audio ON via the app → daemon. Confirm
   audio is captured and a **"Screencap"** row appears under
   System Settings → Privacy → Microphone (attributed to `com.screencap.daemon`).
   This alone tells us whether the daemon can acquire the mic at all.
2. **First unmute of a `--no-audio` recording (R2):** start `--no-audio`, then
   unmute. Observe one of:
   - **Prompt appears** (undetermined) → grant → capture starts. Best case;
     no new grant mechanism needed.
   - **Silently denied** (a background agent often cannot prompt) → the engine
     emits `audio_unmute_failed` and the app surfaces the R3 error (already
     wired in U2/U9). Then a dedicated grant path is required (see below).
   - **Process killed** → the usage string didn't take; re-check the signed
     Info.plist.

## Recommendation for U9 / R2

- **R3 (never silent) is satisfied regardless** of the daemon-grant outcome: U2
  emits the advisory `audio_unmute_failed` when `record_audio` cannot acquire the
  device, and U9 surfaces it. So mute/unmute of an **audio-on** recording (the
  common case — the daemon already holds the grant) is fully shippable now.
- **R2 (unmute a `--no-audio` recording actually starts capture)** is gated on
  step 2 above. If the daemon cannot prompt from the background, the follow-up is
  to **extend `permission.request` with a `microphone` branch** that opens an
  `AVCaptureDevice`/`AVAudioApplication.requestRecordPermission` from the daemon's
  GUI-session agent (the spec notes the helper is "a GUI-session agent able to
  drive TCC prompts"), or a dedicated `mic.request` verb. This is the plan's
  identified scope-expansion vector; scope it only if step 2 shows a silent deny.

**Bottom line:** the daemon needs its own grant (confirmed), the usage-string
prerequisite is now in place, and whether that alone unlocks R2 — or whether a
new daemon-driven prompt path is also required — is a single on-device test that
needs a signed build + a real mic. Ship audio-on mute now; treat R2-on-`--no-audio`
as verified-or-followup pending that test.
