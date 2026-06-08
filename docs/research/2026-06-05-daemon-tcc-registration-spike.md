---
title: "Daemon TCC Registration Spike"
type: research
status: done
date: 2026-06-05
completed: 2026-06-08
plan: docs/plans/2026-06-05-003-feat-daemon-tcc-permission-visibility-onboarding-plan.md
unit: U7
decision: "A (registration works) — SR + Accessibility self-register a toggleable entry from the bare daemon identity on macOS 26.5.1 Tahoe; Input Monitoring needs the real event-tap path or a manual fallback. U8 is buildable."
---

# Daemon TCC Registration Spike (U7)

> **Status: COMPLETE — run on the target Mac 2026-06-08 (macOS 26.5.1 Tahoe,
> M4 Max).** U1–U6 (detection, surfacing, structured failure) shipped
> independently. **Decision: A — a self-service mechanism works.** The bare
> ad-hoc daemon identity, run under launchd, registers a toggleable **Screen
> Recording** and **Accessibility** entry via the request API; **Input
> Monitoring** did not self-register from `IOHIDRequestAccess` alone. The
> feared Tahoe "bare executable can't appear in the Screen Recording list at
> all" regression is **NOT present on 26.5.1**. U8 is buildable — see
> **Decision** and **Net decision & rationale** at the bottom.

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

## Environment (actual values — run 2026-06-08)

| Field | Value |
|---|---|
| macOS product version | **26.5.1 (Tahoe)** |
| Build | **25F80** |
| Hardware | **Apple M4 Max (Mac16,6), arm64** |
| App signing identity | **Apple Development** — `Apple Development: rute.figueiredo92@gmail.com (YL664A67R4)`, hardened runtime; Team `2A8S6MV8DZ`. NOT Developer ID / notarized. |
| Daemon binary signing identity | **ad-hoc** — `screencap` binary `flags=0x2(adhoc)`, `TeamIdentifier=not set`, **no entitlements** |
| `DEVELOPMENT_TEAM` set? | yes (app), but the nested daemon binary is still ad-hoc |
| Run context | Debug `ScreenCap.app` under `.build/ScreenCapDerivedData/…/Debug/`, daemon launched by **launchd via SMAppService** (`submitted by smd.515`) |
| Daemon version running | **0.12.7** (stale — predates U2; `daemon.info` reports no grant block). Identity, not Python version, is what this spike turns on. |

> ⚠️ **This machine is macOS Tahoe (Darwin 25.x / MacOSX26 SDK).** The plan's
> single most severe risk lives here: *"Tahoe 26.1 Background Security
> Improvements can stop a bare Unix executable from appearing in the Screen
> Recording list at all"* — which would defeat **both** the request-API and the
> real-capture paths because they share the same bare daemon identity. Treat the
> **no-registration exit branch** (below) as a live possibility, not a corner case.

## Finding: daemon binary placement & identity (empirically confirmed against the running app — 2026-06-08)

The placement prior is now **confirmed against the actual built+running app**, not
just the repo plist. The running daemon (pid 1418) is:

```
.../Debug/ScreenCap.app/Contents/Resources/screencap-daemon-launcher   (POSIX /bin/sh script)
  └─ exec → Contents/Resources/screencap/screencap serve                (PyInstaller binary)
launchd: program identifier = Contents/Resources/screencap-daemon-launcher (SMAppService)
```

`codesign -dvv` on each artifact:

| Artifact | Identifier | Signature | Team | Entitlements |
|---|---|---|---|---|
| App (`Contents/MacOS/ScreenCap`) | `com.screencap.macos` | Apple Development, hardened runtime | `2A8S6MV8DZ` | (app) |
| `screencap-daemon-launcher` | — (shell script, not Mach-O) | — | — | — |
| `screencap` (the capture/daemon binary) | `screencap-55554944…` | **ad-hoc** (`flags=0x2`) | **not set** | **none** |

So the daemon's TCC subject is a **bare, ad-hoc-signed Unix executable with no
entitlements, living under `Contents/Resources/`, launched by launchd (not spawned
by the running app) — i.e. no responsible-app ancestor.** The app's
Apple-Development identity does **not** extend to it (that extension only happens
for helpers nested at `Contents/MacOS/`, per Apple's "responsible code" model —
Forums thread/694948, thread/692758, Quinn "The Eskimo!").

This is *exactly* the configuration the research says **cannot reliably register a
toggleable Screen Recording entry**, and the one Tahoe 26.1+ "Background Security
Improvements" can block from the Screen Recording list entirely. The runbook below
confirms what actually happens, per pane, on this machine.

**Implication:** the structural prior toward the no-registration branch is strong,
and the real fix is bundle relocation to `Contents/MacOS/` + a responsible-code
ancestor (+ Developer-ID signing) — owned by the Developer-ID distribution plan
(`docs/plans/2026-06-03-001-feat-macos-app-developer-id-notarized-distribution-plan.md`).

## Companion spike tool

> ⚠️ **Confound warning — running this from a terminal tests the WRONG identity.**
> When invoked via `python …spike.py`, the TCC subject is the terminal's
> responsible process (e.g. a granted `claude`/`Claude`/`Terminal`), **not** the
> daemon. This was confirmed: the shell path reported Screen Recording as
> *granted* and captured a real image purely by inheritance. The faithful result
> in this doc came from running the request mechanism **as a launchd agent** under
> a bare ad-hoc identity (see "Running as the daemon — METHOD ACTUALLY USED").
> Use this `.py` only for the `placement`/`check` diagnostics; do not draw
> registration conclusions from its terminal-run output.

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

### Running as the daemon (representative identity) — METHOD ACTUALLY USED

The mechanisms above, run from a terminal, are not faithful to the daemon's TCC
subject: launch context affects attribution, and a shell run lets a present user
grant the prompt interactively, conflating "registered" with "granted." The
daemon is launched by launchd with no granted responsible ancestor — so it must
be tested under launchd. (Observed: the shell run reported `SR=True` at process
start, before any prompt; and when the prompt did appear the operator granted it
interactively — which is why the `screencap` row is ON. Neither reflects the
unattended daemon path. Only the launchd-only, never-granted `screencapspike`
identity gives a clean reading.)

To reproduce the daemon's subject faithfully:

1. **Added a throwaway hidden `screencap _registration-spike <permission>`
   subcommand** (calls `DarwinPlatform.request_*_access()` = Mechanism 1, plus
   `CGDisplayCreateImage`/`CGDisplayStream` for screen_recording = Mechanism 2)
   and **rebuilt the PyInstaller bundle** so the call runs as the ad-hoc
   `screencap` binary identity. *(Reverted from `src/` after the spike; never
   committed. `dist/`+`build/` removed.)*
2. **Ran it under launchd**, not from the shell — a transient GUI-domain
   LaunchAgent (`launchctl bootstrap gui/$UID <plist>`, `RunAtLoad`), exactly the
   daemon's launch context. Under launchd the binary correctly reported
   `SR=False` (no inherited grant) — the faithful baseline.
3. **Used a second, freshly re-signed identity** (`com.screencap.spikeclean`,
   binary renamed `screencapspike`) run **only** under launchd and **never** from
   the shell, to rule out any shell-run contamination of the first identity.
   This is the confound-free result the tables below record.

> The entry that must appear is for the bare `screencap`/daemon code identity,
> not for `python`, `Terminal`, or the granted `claude` responsible process.
> Each distinct ad-hoc cdhash is its own TCC subject (the Accessibility pane
> showed **two** separate `screencap` rows — the daemon's build and the spike
> rebuild — confirming the ad-hoc TCC treadmill).

### Post-grant: no daemon restart needed? (R8)

After toggling the entry **on**, start a *new* recording and confirm it succeeds
**without** `launchctl kickstart` of the daemon (fresh worker reads live TCC —
strongly expected per `session.py`, confirm here):

```bash
screencap start spike-postgrant-test ; sleep 3 ; screencap stop
```

---

## Results (macOS 26.5.1 Tahoe, 2026-06-08)

**Clean run** = fresh identity `com.screencap.spikeclean`, launchd-only, never
shell-run. Default toggle state of a freshly-registered entry is **OFF** (correct
— the daemon registers the row, the user enables it).

| Mechanism | Permission | Entry appeared? | Toggleable? | Default | Notes |
|---|---|---|---|---|---|
| request-api (`CGRequestScreenCaptureAccess`) | screen_recording | ✅ yes | ✅ yes | **OFF** | Clean launchd run registered `screencapspike` in the Screen Recording list. |
| request-api (`AXIsProcessTrustedWithOptions` prompt) | accessibility | ✅ yes | ✅ yes | **OFF** | Clean launchd run registered `screencapspike` in Accessibility. Independently corroborated: the **real running daemon** already had a toggleable `screencap` Accessibility row. |
| request-api (`IOHIDRequestAccess` ListenEvent) | input_monitoring | ❌ no | — | — | No entry appeared from the request API alone under launchd. Returned `False`, registered nothing. Likely needs a real event-tap attempt (`CGEventTapCreate`, the path `pynput` uses) — see notes. |
| real-capture (`CGDisplayCreateImage`) | screen_recording | (not separable) | — | — | Fired (returned a non-nil image even when denied — Tahoe returns a content-limited image), but the request-API already registered the SR entry, so its independent effect wasn't isolated. |
| real-capture (`CGDisplayStream`) | screen_recording | not tested | — | — | The bundled PyObjC `Quartz` binding lacks `dispatch_queue_create`; the stream path never fired. Untested. SR registration did not need it. |
| real-capture | accessibility / input_monitoring | n/a | — | — | No capture analog. |

**Confound observed and ruled out:** the *first* binary (`screencap`) shows in the
SR list toggled **ON** — the operator **manually granted it** when the prompt
appeared (an interactive grant, *not* an automatic/self-grant — this corrects an
earlier misattribution to responsible-process propagation). The clean
`screencapspike` identity, run only under launchd and **never granted**, shows
**OFF**. That OFF entry is the load-bearing evidence: the request API *registers* a
toggleable entry on its own; the grant is a separate user action. Two facts
together: (a) registration works unattended (`screencapspike` OFF), and (b) the
prompt is real and grantable for the daemon identity (`screencap` ON after the
operator approved it).

Post-grant restart needed?

| Question | Answer |
|---|---|
| Does a new recording pick up a freshly-toggled grant without `kickstart`? | **Not live-tested** (would require granting a real permission, which the agent does not do autonomously). **Architecturally yes**: capture runs in freshly-spawned workers whose first in-process `CGPreflight*` reads live TCC state (`src/screencap/session.py` `run_recording_worker`; learning `macos-tcc-per-process-cache-quit-and-relaunch.md`). Confirm in manual QA. |

---

## Decision (drives U8)

Pick exactly one. U8 is built/degraded/deferred accordingly.

- ☑ **A — A self-service mechanism works.** Confirmed empirically on macOS
  26.5.1: the request API registers a toggleable entry from the bare daemon
  identity under launchd for **Screen Recording** (`CGRequestScreenCaptureAccess`)
  and **Accessibility** (`AXIsProcessTrustedWithOptions` prompt), default OFF.
  **U8 implements the `permission.request`-style verb** that calls the matching
  request function per permission in the daemon process, awaits the ack, then
  opens the pane. **Input Monitoring is the exception** — `IOHIDRequestAccess`
  alone did not register; U8 must trigger the real event-tap path for IM (or
  degrade IM's Grant to manual). See Net decision for the per-permission plan.

- ☐ **B — No self-service mechanism registers from the current bundle layout**
  (the Tahoe / `Contents/Resources` outcome). **Not selected** — the feared Tahoe
  "bare executable can't appear in the Screen Recording list at all" regression is
  **not present on 26.5.1**. (Kept here because a future Tahoe point-release could
  reintroduce it; U8's manual-"open Settings" affordance remains the right
  degraded fallback if that happens, and is already the required path for IM.)

### Net decision & rationale

**Chosen branch: A — self-service registration works (with one per-permission carve-out).**
Tested on **macOS 26.5.1 (Tahoe), Apple M4 Max**, against the bare ad-hoc
`screencap` daemon identity (`Contents/Resources/` placement, `TeamIdentifier=not
set`, no entitlements, launchd-launched, no responsible-app ancestor) — i.e. the
exact configuration the external research warned would fail.

**What U8 should build, per permission:**

| Permission | Mechanism for U8's `permission.request` verb | Confidence |
|---|---|---|
| **screen_recording** | Daemon calls `CGRequestScreenCaptureAccess()` in-process → toggleable SR entry appears (default OFF). App awaits ack, opens `Privacy_ScreenCapture`. | High — directly observed (clean launchd identity). |
| **accessibility** | Daemon calls `AXIsProcessTrustedWithOptions({prompt:true})` → toggleable Accessibility entry appears (default OFF). | High — observed twice (clean identity + the real running daemon's pre-existing row). |
| **input_monitoring** | `IOHIDRequestAccess(kIOHIDRequestTypeListenEvent)` **alone did not register** an entry under launchd. U8 should trigger the **real event-tap path** (a `CGEventTapCreate`/`pynput` listener touch — the capture primitive the engine already uses) to force the IM row, **or** degrade IM's Grant to the manual "open Input Monitoring and add ScreenCap" affordance. IM is advisory (not capture-fatal per `capture-health-nonscreen-attribution…`), so a manual fallback here does not block recording. | Medium — request-API path disproven; real-capture path for IM is the open implementation choice for U8. |

**Why this overturns the structural prior:** the placement/identity facts (bare
ad-hoc executable under `Resources/`, no responsible code) created a strong prior
toward branch B, and the research explicitly flagged a Tahoe regression that could
zero out the Screen Recording list for such a binary. **That regression is not in
effect on 26.5.1** — `CGRequestScreenCaptureAccess()` from the launchd daemon
context registered a toggleable SR entry. So registration is not the blocker.

**The real, separate concern is grant *persistence*, not registration.** The
ad-hoc identity churns its cdhash on every rebuild (two `screencap` rows were
visible simultaneously), so a granted entry is orphaned on the next dev build —
the documented TCC treadmill. The shipped **Developer-ID-signed** build has a
stable identity that registers (no worse than the ad-hoc case proven here) **and
persists**. Persistence is owned by the Developer-ID / notarized-distribution plan
(`docs/plans/2026-06-03-001-…`), out of this plan's scope, per Scope Boundaries.

**Responsible-process caveat for U8 (important):** TCC attributes a request to the
*responsible process*. A request issued from a context with an already-authorized
ancestor (e.g. the app, or a terminal) can be attributed there, not to the daemon
(observed: the shell-run binary read `SR=True` at start, before any prompt —
inherited from the granted shell context). U8 must therefore issue the request
**from the daemon's own process** (the `permission.request` verb running
daemon-side), not from the app, so the prompt and entry are attributed to the
daemon identity. This matches the plan's R5 ("the daemon performs the registration
in its own process"). *(Note: the `screencap` row being ON was an operator grant,
not propagation — see the confound note above; this caveat is about request
**attribution/origin**, which the shell `SR=True` preflight independently shows.)*

**R8 (no daemon restart after grant):** not live-tested (granting a real
permission is a user action). Architecturally sound — capture workers are freshly
spawned and read live TCC state (`session.py`); confirm in manual QA by toggling a
pane on and starting a recording without `launchctl kickstart`.

**Leftover test state to clean up (manual — agent cannot modify TCC entries):**
the spike left throwaway rows in System Settings for now-deleted binaries:
`screencap` (Screen Recording, ON — the shell-run rebuild) and `screencap` /
`screencapspike` (Accessibility, OFF; Screen Recording `screencapspike`, OFF).
They are harmless (binaries deleted) but for hygiene remove them via the **–**
button in each pane, or:
`tccutil reset ScreenCapture` / `tccutil reset Accessibility` (note: untargeted
resets also clear other apps' grants, including `claude`/`Claude` — prefer the
per-row **–** button).

## Sources

- Apple Developer Forums: thread/694948, thread/692758 (responsible code, helper
  placement — Quinn "The Eskimo!"); thread/807323, thread/732726 (bare
  `CGRequestScreenCaptureAccess` doesn't list the executable; Tahoe regression).
- ryanthomson.net / nonstrict.eu — real-capture-attempt registration.
- mjtsai.com / lapcatsoftware.com — Sequoia re-auth, `persistent-content-capture`.
- Plan: `docs/plans/2026-06-05-003-feat-daemon-tcc-permission-visibility-onboarding-plan.md` (U7, Risks, External References).
