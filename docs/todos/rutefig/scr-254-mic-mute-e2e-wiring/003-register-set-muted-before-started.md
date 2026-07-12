---
title: Register the set_muted handler before emitting EVENT_STARTED (startup race)
status: open
priority: low
created: 2026-07-12
source: code-review SCR-254 U7-U9 (adversarial finding #3)
related_review_run: /tmp/compound-engineering/ce-code-review/20260712-113628-scr254/
---

## Finding (adversarial, P2, anchor 75 — low practical impact)

The engine emits `EVENT_STARTED` before `record()` registers the `set_muted`
control-channel handler (the handler registers inside `record()` after
`create_recording`, while `started` is emitted earlier). A mute forwarded in
that window is dropped by `control_channel.dispatch_command` ("no handler"),
while `Supervisor.send_command` still returns success (it only checks
`proc.is_alive()`), so the verb reports OK for a command the engine ignored.

## Why this wasn't auto-fixed here / low priority

The window is milliseconds and the app only exposes the mute control once it is
in `.recording` state, so a user toggling inside it is unrealistic. The
confirmed-state design keeps it honest — a dropped early mute simply never
confirms, so the app stays unmuted and the user can retry. Fixing it cleanly
means either registering the handler earlier (it needs `recording` + the mute
queue, which exist only after `create_recording`) or gating `recording.mute` on a
handler-ready signal so an early mute returns a retriable `not_recording` rather
than a silent-success drop.

## Next step for the PR author

Prefer gating the verb: have `send_command`/`set_muted` (or the `recording.mute`
route) confirm a `set_muted` handler is registered before reporting success, so
an early mute is retriable rather than silently dropped. Add a daemon test for
the started-before-handler window.
