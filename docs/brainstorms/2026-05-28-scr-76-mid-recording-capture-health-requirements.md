---
date: 2026-05-28
topic: scr-76-mid-recording-capture-health
---

# SCR-76: Mid-Recording Capture Health Detection

## Summary

Replace the broken subprocess-based TCC probe in the daemon's mid-recording revocation watcher with a capture-side health signal that observes whether the recording is producing useful events, attributed via an in-process Quartz lookup only when unhealth fires. The existing `permission_lost` UX contract is preserved; the broken probe path is removed from the mid-recording surface.

---

## Problem Frame

Daemon recordings in the bundled `Screencap.app` have been silently producing useless captures — audio captures correctly but the screen-side event tables (action, window, screenshot) are empty across recent recordings where the user was actively interacting. The user has lived through this failure mode without any signal from the app.

The proximate cause for the mid-recording watcher's silence is documented in SCR-76: the watcher's TCC probe spawns `sys.executable -c <code>`, but in the frozen daemon `sys.executable` is the bundled Click CLI which rejects `-c`, returns an empty stdout, and the probe falls back to its tri-state "couldn't determine" result. The watcher's fail-open policy then skips the tick, silently. The same class of broken-in-frozen-but-green-in-tests defect already produced SCR-69 — the existing test machinery never exercises the actual entry point under frozen-mode dispatch, so probe-shape failures don't surface in CI.

The underlying cause of the production silent failures may be TCC denial, a Quartz hiccup, dead reader threads, or a fresh-build-identity-invalidates-TCC-grant — the probe gives the user no help discriminating between them, because the probe is a proxy for the symptom rather than an observation of it.

---

## Requirements

**Capture-side health detection**

- R1. The recording engine observes the gap between *reader attempts* and *successful writes* per reader (action, window, screen) over a rolling time window. A reader is unhealthy when it is demonstrably attempting capture but consistently producing no useful output. Unhealth is per-reader and bubbles up to a recording-level signal when any reader is unhealthy.
- R2. The health check runs in the engine process and consumes per-reader attempt and output counters maintained inside the engine; it MUST NOT introduce new IPC between daemon, engine, and SwiftUI shell.
- R3. The detection window can be short (seconds, not minutes) because legitimate user idle is structurally distinguishable from capture-broken: idle = readers attempt and write nothing because there is nothing to capture; broken = readers attempt but the call returns empty / fails / produces zero useful output. Exact window length is a plan-time calibration decision.
- R4. The health check tolerates transient Quartz / PyObjC failures in any attribution call without killing the recording (fail-open contract). It also tolerates a single tick of attempt-without-output without firing (debounce); the unhealth signal requires a sustained gap over the window.

**Cause attribution**

- R5. When the health watcher fires, the engine performs an in-process Quartz / Accessibility check to label which TCC permission (Screen Recording, Accessibility, Input Monitoring) is the most likely cause.
- R6. The labeller's per-process TCC cache limitation is acceptable: the cache reflects state at engine start, which is sufficient for attribution after a symptom has already been observed independently.
- R7. When attribution is inconclusive (Quartz import fails, or all permissions report granted), the engine emits a cause-agnostic health signal rather than guessing.

**UX contract preservation**

- R8. When a TCC permission is identified as the cause, the engine emits the existing `permission_lost` stderr event with the correct `permission=` value, preserving the SwiftUI shell's existing recorder-controller behavior on the deny path.
- R9. When the cause is not identified as TCC (or attribution is inconclusive), the engine emits a distinct, cause-agnostic health event (separate from `permission_lost`) so the shell can present a different UX from the per-permission deny path. The exact event name is a plan-time decision; the contract is that it is a NEW event type, not a `permission_lost` variant.

**Removal of broken probe surface**

- R10. The mid-recording revocation watcher no longer calls the existing subprocess-based TCC probe; the new capture-side mechanism fully replaces it for the daemon recording path.

**Test discipline**

- R11. At least one test asserts the system's health-detection behavior by driving the engine with a reader stub that simulates "attempting but producing nothing" and verifies the unhealth event fires. Idle (attempts and writes both zero, OR attempts proportional to writes) MUST NOT trigger the unhealth event in tests.
- R12. The frozen-daemon dispatch path is exercised end-to-end by at least one test (or test harness) that uses the actual bundled-binary entry-point semantics, so the broken-in-frozen-mode-but-green-in-tests regression class cannot recur silently for the mid-recording watcher.

---

## Acceptance Examples

- AE1. **Covers R1, R8.** Given a daemon recording is running and Screen Recording is denied for the daemon's bundle identity, when the screen reader's attempt counter advances over the window but its output counter stays at zero, the engine emits a `permission_lost` stderr event with `permission="screen_recording"`.
- AE2. **Covers R1, R9.** Given a daemon recording is running and a reader thread has died (attempts also zero), when the window elapses with no attempts AND no output from that reader, the engine emits the new cause-agnostic capture-unhealthy event — not `permission_lost`.
- AE3. **Covers R3.** Given a daemon recording is running and the user is legitimately idle (no keyboard or mouse activity), when the window elapses with the action reader attempting and producing zero events *proportionally* (idle is the normal state, attempts produce zero because there is no input to capture, not because the call failed), no unhealth signal is emitted. Idle is distinguished from broken by reader-level behavior, not by the window length.
- AE4. **Covers R4, R7.** Given the health watcher has fired and the in-process Quartz call raises, the engine emits the cause-agnostic capture-unhealthy event and the recording continues; the labeller's failure does not kill the recording.

---

## Success Criteria

- A user who toggles off Screen Recording in System Settings during a daemon recording receives a visible UX signal (existing `permission_lost` event surfaced by the shell) within the health window.
- A user whose daemon recording is silently producing useless captures (current production behavior) receives a visible UX signal regardless of the underlying cause — they no longer end up with a recording directory that has only audio.
- The replacement mechanism passes a test that observes real event-table state under frozen-daemon dispatch semantics, structurally preventing the broken-in-frozen-mode-but-green-in-tests regression class for the mid-recording watcher.
- A downstream planner can read this doc and pick the health window default, the labeller fallback ordering, and the cause-agnostic event name without re-running the brainstorm.

---

## Scope Boundaries

- The CLI first-run prompt-loop's frozen-mode breakage (the other caller of the subprocess probe, triggered only on standalone CLI installs before recording begins) is NOT addressed; that surface keeps the existing broken probe behavior until a separate ticket.
- The originally-proposed hidden `_probe-permission` Click subcommand is NOT introduced for mid-recording detection.
- Moving the watcher into the SwiftUI shell with a new daemon-socket protocol for shell-driven recording termination is NOT pursued.
- `TCC.db` kqueue / FSEvents observation is NOT pursued.
- The startup preflight (already correctly handled by PR #193's in-process Quartz call in the freshly-spawned worker) is NOT re-litigated.
- The fresh-build-identity-invalidates-TCC-grant UX problem (a plausible underlying cause for what has been hitting production) is NOT addressed here; it would warrant its own ticket if confirmed.
- The unrelated `n_uploaded` NameError in the recording cleanup path is NOT addressed; spawned as a separate task.
- A separate interactive-vs-passive recording mode is NOT introduced. The reader-attempt-vs-output design makes mode tagging unnecessary for the health signal — idle and broken are distinguishable at the reader level on every recording.

---

## Key Decisions

- **Capture-side health detection chosen over fixing the active subprocess probe.** The active probe only catches TCC denial; a capture-side signal catches all causes of silent recording failure. Production evidence — 7 recent daemon recordings showing the silent-failure mode despite active user interaction — established that the broader problem matters more than the narrower one. The signal IS the symptom rather than a proxy, which structurally prevents the regression class.
- **Reader-attempt-vs-output, not raw output, drives the health signal.** A first-pass design that watched only "did events get written?" would conflate capture-broken with user-idle, forcing a long window to avoid false positives. Instrumenting each reader's *attempt counter* alongside its *output counter* lets the engine distinguish "reader is alive and calling capture APIs but getting nothing useful back" (broken) from "reader is alive and getting back legitimately-empty results" (idle). Detection latency drops back toward seconds; long-passive-recording false positives are structurally prevented rather than tuned around.
- **In-process Quartz used as a labeller, not a primary detector.** The per-process TCC cache makes Quartz unreliable as a live probe inside the long-running recorder, but acceptable as an attribution hint after a symptom has already been observed independently.
- **Existing `permission_lost` stderr event preserved as the deny-path signal; non-TCC unhealth gets a NEW event type.** The SwiftUI shell does not need a new event handler for the TCC case. For the non-TCC unhealth path, a distinct new event type is introduced rather than re-using `permission_lost` with a sentinel `permission="unknown"` value. Rationale: `permission_lost` semantically implies attribution succeeded; overloading it to also mean "we don't know" blurs the contract and forces shell-side branching on a value sentinel rather than an event-type discriminator. A new event type lets the shell pattern-match cleanly and present a deliberately distinct UX (the TCC deny case has a different remediation than "your readers died for unknown reasons").
- **The existing subprocess probe is not deleted.** Its other caller — the CLI first-run prompt loop — still depends on it. Only the mid-recording caller is replaced.

---

## Dependencies / Assumptions

- The engine already maintains per-event-type output counters accessible to a watcher running in the same process. **New counter contract required:** each reader (action, window, screen) must also expose an attempt counter (incremented before the capture call) in addition to its existing output counter (incremented on a successful write). The health watcher consumes both. This is the only new internal wiring introduced by this change.
- The SwiftUI shell's `RecorderController` already consumes `permission_lost` stderr events with a `permission=` field and routes the UX accordingly; preserving this contract is sufficient.
- macOS in-process Quartz / Accessibility lookups remain available in the frozen daemon binary for use as a labeller (the per-process cache caveat applies but does not block this use).
- The daemon's existing fail-open contract on any TCC-adjacent call is preserved; nothing in this ticket can kill a recording because a Quartz call failed.

---

## Outstanding Questions

### Resolve Before Planning

*(All previously-blocking questions are resolved. The remaining items are planner-shape decisions.)*

### Deferred to Planning

- [Affects R1][Technical] Where the attempt counter increment lives in each reader's loop — before the capture call, after a non-error return, or after a content-validation step. Affects whether "Quartz returned an empty buffer" counts as an attempt-with-no-output (broken) or an attempt-with-empty-output (could be idle if the reader can't tell).
- [Affects R3][Technical] Exact health window length and debounce semantics — short enough to surface a real failure quickly, long enough to swallow a single transient zero-tick. Planner picks based on reader cadences (e.g., screen reader's ~20 fps vs action reader's event-driven cadence).
- [Affects R5][Needs research] Per-bundle-identity TCC semantics for the bundled daemon binary: confirm whether `Quartz.CGPreflightScreenCaptureAccess` from within the daemon process reports against the daemon's bundle identity or the parent app's. Drives labeller correctness.
- [Affects R12][Technical] Frozen-daemon test harness shape: invoke the actual built binary in tests, or simulate frozen-mode dispatch in a lighter-weight way that still exercises the entry-point boundary.
