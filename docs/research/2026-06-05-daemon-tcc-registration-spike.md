---
title: "Daemon TCC Registration Spike"
type: research
status: in-progress
date: 2026-06-05
plan: docs/plans/2026-06-05-003-feat-daemon-tcc-permission-visibility-onboarding-plan.md
unit: U7
---

# Daemon TCC Registration Spike (U7)

> **Status: TEMPLATE / RUNBOOK — awaiting a human run.** U1–U6 (detection,
> surfacing, structured failure) shipped independently. **U8 (the daemon-driven
> registration verb) is gated on the decision recorded at the bottom of this
> doc.** Run the steps below on the target Mac, fill in the result tables, then
> pick the **Decision** so U8 can be built, degraded, or deferred.

## Why this spike exists

R5/R6 want: when the user clicks **Grant**, the daemon registers *itself*
(`com.screencap.daemon`, the TCC subject) so it becomes a **toggleable entry**
in the relevant System Settings pane. The central empirical unknown (the plan's
only `[Needs research]`) is **which mechanism actually produces that toggleable
entry from the daemon's real runtime context** — and whether *any* does on this
macOS.

External research (Apple DTS, OSS screen recorders) says the bare
request-API path is unreliable from a background helper and may be regressed on
recent macOS, so the **real-capture-attempt** path and the
**bundle-placement / "responsible code"** question are first-class spike
subjects — not fallback comments.

## Environment (fill in actual values)

| Field | Value |
|---|---|
| macOS product version | _e.g. 26.1 (run `sw_vers -productVersion`)_ |
| Build | _`sw_vers -buildVersion`_ |
| Hardware | _Apple Silicon / Intel_ |
| App signing identity | _ad-hoc (dev) / Developer ID / notarized_ |
| `DEVELOPMENT_TEAM` set? | _yes/no_ |
| Run context | _Xcode-run `.app` / installed `.app` / dev CLI_ |

> ⚠️ **This machine is macOS Tahoe (Darwin 25.x / MacOSX26 SDK).** The plan's
> single most severe risk lives here: *"Tahoe 26.1 Background Security
> Improvements can stop a bare Unix executable from appearing in the Screen
> Recording list at all"* — which would defeat **both** the request-API and the
> real-capture paths because they share the same bare daemon identity. Treat the
> **no-registration exit branch** (below) as a live possibility, not a corner case.

## Pre-filled finding: daemon binary placement (verified from the repo)

The daemon LaunchAgent runs the binary from **`Contents/Resources/`**, not
`Contents/MacOS/`:

```
# macos/ScreenCap/Resources/com.screencap.daemon.plist
BundleProgram = Contents/Resources/screencap-daemon-launcher
```

Apple's documented "responsible code" fix (Forums thread/694948, thread/692758,
Quinn "The Eskimo!") is to embed the helper at **`App.app/Contents/MacOS/`** so
TCC grants the privilege to the app and extends it to the nested helper. A
binary under `Contents/Resources/` with no responsible-app ancestor is the exact
configuration the research says **cannot reliably register a toggleable entry**.

**Implication:** even before running anything, there is a strong prior that
self-service registration will fail from the current bundle layout, and that the
real fix is bundle relocation — owned by the Developer-ID distribution plan
(`docs/plans/2026-06-03-001-feat-macos-app-developer-id-notarized-distribution-plan.md`).
The runbook below confirms this empirically and records whether any mechanism
works *despite* the placement.

## Companion spike tool

`docs/research/2026-06-05-daemon-tcc-registration-spike.py` — a **throwaway**
script (NOT production code; delete after the spike). It exercises each mechanism
so you can run it, then look at System Settings. Usage:

```bash
# From the repo root, dev identity (terminal's TCC subject):
PYTHONPATH=src python docs/research/2026-06-05-daemon-tcc-registration-spike.py <mechanism> [permission]

# mechanisms: request-api | real-capture | placement | check
# permission: screen_recording | accessibility | input_monitoring  (default: screen_recording)
```

To exercise the **daemon's** identity (the representative case), run the same
mechanism through the bundled binary as the daemon — see "Running as the daemon"
below.

---

## Runbook

Reset to a clean state before each mechanism so a stale grant doesn't mask the
result:

```bash
# Clean the daemon's TCC entry (and the app's, if testing the app subject).
tccutil reset ScreenCapture com.screencap.daemon
tccutil reset ScreenCapture com.screencap.macos
tccutil reset Accessibility com.screencap.daemon
tccutil reset ListenEvent com.screencap.daemon
# Restart the daemon so it picks up a clean slate (LaunchAgent-managed):
launchctl kickstart -kp gui/$(id -u)/com.screencap.daemon
```

For each mechanism: run it, then open the matching pane and record whether a
**`ScreenCap helper` / `screencap-daemon-launcher` entry appears** and whether it
is **toggleable**.

```bash
open "x-apple.systempreferences:com.apple.settings.PrivacySecurity.extension?Privacy_ScreenCapture"
open "x-apple.systempreferences:com.apple.settings.PrivacySecurity.extension?Privacy_Accessibility"
open "x-apple.systempreferences:com.apple.settings.PrivacySecurity.extension?Privacy_ListenEvent"
```

### Mechanism 1 — Bare request API (most likely to fail per research)

`DarwinPlatform.request_*_access()` in the daemon process:
`CGRequestScreenCaptureAccess()` / `IOHIDRequestAccess(kIOHIDRequestTypeListenEvent)` /
`AXIsProcessTrustedWithOptions({prompt: true})`.

```bash
PYTHONPATH=src python docs/research/2026-06-05-daemon-tcc-registration-spike.py request-api screen_recording
```

### Mechanism 2 — Real minimal capture attempt

Initiate an actual capture touch (CGDisplayStream / a real `CGDisplayCreateImage`)
so TCC sees a genuine capture, which research says is more likely to force the
entry to appear. Requires an active Aqua login session (login-window context
returns null frames).

```bash
PYTHONPATH=src python docs/research/2026-06-05-daemon-tcc-registration-spike.py real-capture screen_recording
```

### Mechanism 3 — Bundle placement / responsible code (diagnostic)

Confirm where the daemon binary lives in the built `.app` and its code identity.

```bash
PYTHONPATH=src python docs/research/2026-06-05-daemon-tcc-registration-spike.py placement
# and, against a built app:
codesign -dvvv "/path/to/ScreenCap.app/Contents/Resources/screencap-daemon-launcher" 2>&1 | sed -n '1,20p'
```

### Running as the daemon (representative identity)

The mechanisms above, run from a terminal, register the *terminal's* TCC subject.
To test the **daemon's** subject, trigger the mechanism from the daemon process.
Options (record which you used):

- Temporarily add a hidden `screencap _registration-spike` subcommand and invoke
  it via the bundled binary under the daemon's LaunchAgent identity. *(Throwaway —
  do not commit to `src/`.)*
- Or have the daemon shell out to the spike script with its own argv during a
  manual test session.

> The point is: the entry that must appear is for `com.screencap.daemon` /
> `screencap-daemon-launcher`, not for `python` or `Terminal`.

### Post-grant: no daemon restart needed? (R8)

After toggling the entry **on**, start a *new* recording and confirm it succeeds
**without** `launchctl kickstart` of the daemon (fresh worker reads live TCC —
strongly expected per `session.py`, confirm here):

```bash
screencap start spike-postgrant-test ; sleep 3 ; screencap stop
```

---

## Results (fill in)

Per mechanism × permission × the entry that appeared:

| Mechanism | Permission | Entry appeared? | Toggleable? | Notes |
|---|---|---|---|---|
| request-api | screen_recording | ☐ | ☐ | |
| request-api | accessibility | ☐ | ☐ | |
| request-api | input_monitoring | ☐ | ☐ | |
| real-capture | screen_recording | ☐ | ☐ | |
| real-capture | accessibility | ☐ | ☐ | n/a? a11y has no "capture" analog |
| real-capture | input_monitoring | ☐ | ☐ | |

Post-grant restart needed?

| Question | Answer |
|---|---|
| Does a new recording pick up a freshly-toggled grant without `kickstart`? | ☐ yes / ☐ no |

---

## Decision (drives U8)

Pick exactly one. U8 is built/degraded/deferred accordingly.

- ☐ **A — A self-service mechanism works.** Record which mechanism registered a
  toggleable entry for each permission, on this macOS. **U8 implements that path**
  (with the real-capture path as a first-class fallback), wires the Grant button
  to a daemon `permission.request`-style verb, awaits the ack, then opens the pane.

- ☐ **B — No self-service mechanism registers from the current bundle layout**
  (the Tahoe / `Contents/Resources` outcome). **U8 is blocked**; a real fix
  depends on the Developer-ID / bundle-relocation work (daemon embedded at
  `App.app/Contents/MacOS/` with a responsible-code ancestor) owned by the
  Developer-ID distribution plan. **U8 degrades** the Grant button to an honest
  "open Settings and enable the ScreenCap helper entry manually" affordance.
  Detection (U1–U3), surfacing (U4–U5), and structured-failure (U6) — already
  shipped — stand on their own.

### Net decision & rationale

_Fill in: chosen branch, the exact mechanism (if A), per-permission notes, the
macOS version tested, and whether a post-grant restart was required._

## Sources

- Apple Developer Forums: thread/694948, thread/692758 (responsible code, helper
  placement — Quinn "The Eskimo!"); thread/807323, thread/732726 (bare
  `CGRequestScreenCaptureAccess` doesn't list the executable; Tahoe regression).
- ryanthomson.net / nonstrict.eu — real-capture-attempt registration.
- mjtsai.com / lapcatsoftware.com — Sequoia re-auth, `persistent-content-capture`.
- Plan: `docs/plans/2026-06-05-003-feat-daemon-tcc-permission-visibility-onboarding-plan.md` (U7, Risks, External References).
