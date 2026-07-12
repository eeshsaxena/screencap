---
title: Carry audio-on/off on the session snapshot so a --no-audio reconnect isn't "Mic on"
status: open
priority: medium
created: 2026-07-12
source: code-review SCR-254 U7-U9 (adversarial finding #2)
related_review_run: /tmp/compound-engineering/ce-code-review/20260712-113628-scr254/
---

## Finding (adversarial, P2, anchor 75)

On app reconnect/relaunch onto a live `--no-audio` recording, the session
snapshot hydrates `muted` but NOT `audioEnabled` (which defaults `true`, and is
only set on start or on an `audio_unmuted` event). So the effective-mute display
(`MuteControlPresentation.effectivelyMuted = muted || !audioEnabled`) computes
`false` → the control reads **"Mic on"** for a recording that has no mic capture,
and tapping it dispatches a mute that the engine no-ops (already not capturing) →
no confirming event → the control wedges (see todo 001).

## Why this wasn't auto-fixed here

The fix is cross-stack and needs a daemon change:
1. Persist the effective `audio` state in the daemon session state at start.
2. Add `audio` to the `session.snapshot` overlay (symmetric with the `muted`
   overlay already added in U5, `daemon/app.py`).
3. Decode it in Swift `SessionSnapshotResponse` (additive, absent → true for
   back-compat) and hydrate `audioEnabled` on attach/reconnect.

The Swift half can't be compiled/tested in this worktree (xcodebuild TCC hazard),
and the daemon-state persistence needs its own test, so this is deferred rather
than landed blind.

## Next step for the PR author

Implement the three points above; add a daemon snapshot test asserting `audio`
is surfaced for a `--no-audio` recording, and a `RecorderControllerDaemonTests`
case asserting a `--no-audio` reconnect shows the muted/"Mic off" affordance (not
"Mic on") and that a tap dispatches an *unmute*, not a no-op mute.
