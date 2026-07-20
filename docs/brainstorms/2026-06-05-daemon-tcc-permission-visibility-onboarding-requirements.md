---
date: 2026-06-05
topic: daemon-tcc-permission-visibility-onboarding
---

# Daemon TCC Permission Visibility & Onboarding

## Summary

Make the macOS app aware of the recording daemon's real TCC grant state, surface the permission walkthrough exactly when the daemon is missing a required grant, let the daemon register itself in System Settings on a deliberate user action, and guarantee a recording can never silently abort when permission is missing.

---

## Problem Frame

Screencap's macOS app (`com.screencap.macos`) is a SwiftUI shell; the actual screen capture is done by a separate background daemon (`com.screencap.daemon`, the bundled `screencap serve` process) running under its own LaunchAgent and talking to the app over a UNIX socket. The daemon — not the app — is the TCC identity that must hold Screen Recording, Accessibility, and Input Monitoring.

The app's onboarding assumes **"daemon socket reachable ⟹ permissions are fine."** That assumption is false, and when it breaks the user is stranded with no recoverable path:

- The first-run permission walkthrough is gated to appear only when the daemon is **unreachable** (`macos/Screencap/Views/MainWindow.swift` — `transport == .cliFallback`). In the normal post-setup state the daemon is reachable, so the daemon-permission walkthrough — the only UI that grants the daemon's permissions — never appears.
- The app cannot register the daemon in TCC on the daemon's behalf (`macos/Screencap/Controllers/PermissionController.swift` `requestAndOpenSettings(subject: .daemon)` opens Settings but never calls a request API), so the daemon never shows up in the Screen Recording pane for the user to toggle.
- The app has no visibility into the daemon's grant state (`CLIStatus` / `daemon.info` carry no permission fields), so in daemon mode `start()` skips its permission guard, dispatches a start, and the daemon engine aborts via `permission_policy.preflight()` → `raise SystemExit(1)` (`src/screencap/recorder.py`) into an empty `serve.log`.

The observed cost: a developer or tester whose daemon was rebuilt or whose grant was orphaned cannot start a recording, sees no grant prompt, and finds no entry to approve in System Settings — with no in-product way out. This pain is permanent under the current design, not a transient state. It also silently breaks the re-grant assumption in the active Developer-ID distribution plan (`docs/plans/2026-06-03-001-feat-macos-app-developer-id-notarized-distribution-plan.md`, U5), whose post-flip flow assumes the walkthrough appears after a `tccutil reset`.

---

## Actors

- A1. User (developer / tester / operator): grants permissions in System Settings and starts recordings.
- A2. Screencap app (`com.screencap.macos`): the GUI shell; presents onboarding, reads the daemon's reported state, opens Settings panes, coordinates daemon refresh. Cannot register the daemon's TCC identity itself.
- A3. Screencap daemon (`com.screencap.daemon`): the capture process and TCC subject; the only actor that can register itself in System Settings and authoritatively know its own grant state.
- A4. macOS TCC / System Settings: holds the grants, keyed on the daemon's code identity; shows a toggleable entry only for processes that have registered as screen-capture clients.

---

## Key Flows

- F1. First-run grant (daemon reachable, never granted)
  - **Trigger:** App launches; daemon is installed and reachable but reports a required permission as not granted.
  - **Actors:** A1, A2, A3, A4
  - **Steps:** App reads the daemon's grant state → app presents the permission walkthrough → user clicks Grant for a permission → daemon registers itself for that permission (so it appears in Settings) → app opens the matching Settings pane → user toggles the daemon entry on → daemon observes the new grant → app reflects granted.
  - **Outcome:** All three required grants read as granted; recording can start.
  - **Covered by:** R1, R2, R3, R5, R6, R9

- F2. Recovery / re-grant (previously granted, now lost)
  - **Trigger:** A grant was orphaned (daemon rebuild / signing change / user revocation) while the daemon is reachable.
  - **Actors:** A1, A2, A3, A4
  - **Steps:** Same detection + surfacing + registration path as F1 — the app does not distinguish "never granted" from "lost"; the missing-grant signal alone drives the walkthrough.
  - **Outcome:** Grants restored without any manual `tccutil reset` / stop-daemon ritual.
  - **Covered by:** R1, R3, R5, R8, R9

- F3. Start attempted while a grant is missing
  - **Trigger:** A recording start reaches the daemon while a required permission is missing (e.g. revoked between checks).
  - **Actors:** A1, A2, A3
  - **Steps:** Daemon returns a structured "permission required" result naming the missing permissions → app surfaces it and routes the user into the grant flow (F1/F2) → no silent abort.
  - **Outcome:** The user always sees why the recording didn't start and what to do.
  - **Covered by:** R4, R7

---

## Requirements

**Detection — the daemon reports its own state**
- R1. The daemon reports its current grant state for the three required permissions (Screen Recording, Accessibility, Input Monitoring) to the app over the existing daemon-info channel, using silent in-process preflight checks that neither prompt nor mutate state.
- R2. The app reads the daemon's reported grant state when it connects and refreshes it while onboarding is visible, so the app's permission view reflects the daemon's state — not the app process's own (irrelevant) TCC state.
- R9. The state the daemon reports must reflect **live** TCC state, not a value cached at daemon-process launch (the daemon process caches TCC like any other; see `docs/solutions/runtime-errors/macos-tcc-per-process-cache-quit-and-relaunch.md`). There is prior art for a fresh-check mechanism in the engine (`_check_permission_fresh`).

**Surfacing — gate onboarding on real state**
- R3. The permission walkthrough is presented whenever the daemon is reachable but reports a missing required grant — it is no longer gated on the daemon being unreachable.
- R4. Before starting a daemon-backed recording, the app blocks and routes the user into the grant flow when the daemon reports a missing required grant, rather than dispatching a start that the engine will reject.

**Registration — on-demand, daemon-driven (Approach B)**
- R5. When the user acts to grant a daemon-owned permission, the daemon performs the registration in its own process (calls that permission's system request API) so the daemon becomes a toggleable entry in the relevant System Settings pane; the app triggers this action and then opens the matching pane. This replaces the current no-op app-side `subject: .daemon` path.
- R6. Registration is supported for each of the three required permissions via that permission's matching request mechanism (not Screen Recording alone).

**Failure visibility — safety net**
- R7. A daemon recording-start attempt that lacks a required grant returns a structured, typed "permission required" result over IPC that identifies which permissions are missing, instead of aborting silently; the app surfaces it to the user. The daemon engine must not terminate the start with an unobservable `SystemExit` on the daemon transport.

**Recovery parity**
- R8. First-run (never granted) and recovery (previously granted, then lost) resolve through the same detection → surfacing → registration path; the design does not require the user to run a manual reset or stop-the-daemon ritual to recover in a shipped build.

---

## Acceptance Examples

- AE1. **Covers R1, R2, R3.** Given the daemon is reachable but missing Screen Recording, when the app launches, then the permission walkthrough is presented (not suppressed).
- AE2. **Covers R5, R6.** Given the walkthrough is showing and the daemon lacks Screen Recording, when the user clicks Grant, then the daemon registers as a screen-capture client and a toggleable Screencap-helper entry appears in the Screen Recording pane the app opens.
- AE3. **Covers R4, R7.** Given a required grant is missing, when a recording start reaches the daemon, then the daemon returns a structured "permission required" result naming the missing permission(s) and the app surfaces it and routes to the grant flow — `serve.log` is not left as the only (empty) trace.
- AE4. **Covers R8, R9.** Given a grant was orphaned by a daemon rebuild while the daemon is reachable, when the user re-grants via the walkthrough, then the app reflects granted after the daemon observes live state — without any `tccutil reset` or manual daemon stop.

---

## Success Criteria

- A user whose daemon lacks a required grant is always shown the walkthrough and can complete granting entirely in-product — no terminal, no manual reset.
- The daemon always appears as a toggleable entry in the relevant System Settings pane once the user has acted to grant it.
- A recording start never silently fails on missing permission: every failure is a visible, actionable message.
- The app's displayed permission state matches what the daemon can actually do (no app-vs-daemon drift).
- Downstream handoff: `ce-plan` can implement without inventing product behavior — the detection channel, the on-demand registration trigger, the gating change, and the structured-error contract are all specified at the decision level; the open items are the registration-reliability spike and the post-grant daemon-refresh mechanism (below), explicitly flagged.

---

## Scope Boundaries

- Developer ID signing, notarization, and the ad-hoc-rebuild "TCC treadmill" persistence fix — owned by `docs/plans/2026-06-03-001-feat-macos-app-developer-id-notarized-distribution-plan.md`. This work assumes grant persistence is handled there and only fixes detection / surfacing / registration.
- Collapsing capture into the app's own identity / removing the separate daemon TCC subject — rejected; the daemon stays the permission-holding identity.
- The dev-only `tccutil reset` + stop-daemon recovery ritual — remains a developer convenience, not the product recovery path.
- Login Items / SMAppService helper approval — a separate, already-handled `permission_required` concern (`src/screencap/daemon/launchagent.py`); not part of this redesign.
- Microphone permission — optional; capture does not gate on it.
- Startup self-registration (the daemon calling request APIs on every `serve` boot, Approach A) — not chosen; registration is on-demand.

---

## Key Decisions

- Daemon (`com.screencap.daemon`) remains the TCC subject: consistent with the active Developer-ID distribution plan's Key Decision; the app cannot and should not become the capture identity.
- Registration is on-demand (Approach B), triggered by an explicit user Grant action, not a blunt every-boot request: ties the request to intent, avoids firing the request API on every daemon launch, and is the clean replacement for today's no-op `subject: .daemon` path.
- The structured start-error safety net is non-negotiable across the design: it is what guarantees a recording can never again silently abort, and it makes the daemon the single source of truth for start-time permission failures.
- Detection rides the existing `daemon.info` channel rather than a new status surface: the app already probes it on connect, so adding a permission block is the smallest coherent change.

---

## Dependencies / Assumptions

- Depends on the Developer-ID distribution plan for grant **persistence** across rebuilds; without it, dev builds still hit the treadmill (this work makes that state recoverable in-product, but does not stop the orphaning).
- Assumes the daemon can read live TCC state cheaply via an in-process or fresh-subprocess preflight (prior art: `_check_permission_fresh`, `DarwinPlatform.is_screen_recording_enabled`).
- Assumes a per-user LaunchAgent calling the request API can register a System Settings entry for its identity — this is the central empirical unknown (see Outstanding Questions).

---

## Outstanding Questions

### Deferred to Planning

- [Affects R5][Needs research] Does a background per-user LaunchAgent calling the Screen Recording request API reliably (a) register a toggleable Settings entry and (b) optionally surface a prompt? Validate empirically; if registration is unreliable from the daemon context, fall back to triggering registration via a real minimal capture attempt. This unknown exists regardless of trigger choice and should be spiked early.
- [Affects R8, R9][Technical] After the user toggles the grant in Settings, must the running daemon be restarted (launchctl kickstart, mirroring the app's Quit & Relaunch) for capture to succeed, or does a fresh-subprocess capture path pick up the new grant without a restart? Determines whether the app must coordinate a daemon refresh after granting.
- [Affects R1][Technical] Exact placement of the permission block on the daemon-info contract and the corresponding app-side decode + schema-version bump.
- [Affects R7][Technical] The typed "permission required" envelope shape and how the app maps it onto the existing recorder error/event surface.
