---
title: Add a muteInFlight watchdog so an unconfirmed toggle self-heals
status: open
priority: medium
created: 2026-07-12
source: code-review SCR-254 U7-U9 (adversarial finding #1)
related_review_run: /tmp/compound-engineering/ce-code-review/20260712-113628-scr254/
---

## Finding (adversarial, P2, anchor 75)

`RecorderController.muteInFlight` (macOS `RecorderController.swift`) is cleared
ONLY by a confirming `audio_muted`/`audio_unmuted` event, a transport failure,
or the `.idle` reset. If the daemon forwards a `recording.mute` command that
returns 200 but the engine never emits a confirming event, the HUD/menu control
wedges at "Muting…/Unmuting…" (disabled) for the rest of the recording.

## Why this wasn't auto-fixed here

The two *reachable* causes of an unconfirmed toggle were fixed in this PR:
- the teardown-drop of a queued mute (engine drain, `recorder.py`), and
- the double-`requestAccess` during the permission prompt (`beginUnmute` now arms
  `muteInFlight`).

A general watchdog is a defense-in-depth net for any *other* no-confirm path
(e.g. the `--no-audio`-reconnect no-op in todo 002, or an engine that stops
emitting). It adds a new timer/Task + cancellation surface to `RecorderController`,
which cannot be compiled/tested in this worktree (xcodebuild TCC hazard), so it is
deferred to a build-capable session rather than landed blind.

## Next step for the PR author

Add a bounded reconciliation: when `muteInFlight` is set, start a ~4-5 s timer
(cancelled by the confirming event); on expiry, clear `muteInFlight` and re-read
the session snapshot's confirmed `muted` so the control recovers. Cover it with a
`RecorderControllerDaemonTests` case (toggle with no confirming event → control
re-enables after the timeout). Consider whether the daemon should also surface a
timeout/failure event so the app doesn't rely on a client-side timer alone.
