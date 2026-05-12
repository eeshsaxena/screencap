---
title: "Late-listener races on the daemon EventBus — bounded replay buffer closes the gap"
slug: eventbus-late-listener-replay-2026-05-12
date: 2026-05-12
category: runtime-errors
severity: high
problem_type: runtime-error
modules:
  - src/screencap/daemon/event_bus.py
  - src/screencap/daemon/supervisor.py
  - src/screencap/daemon/app.py
  - macos/ScreenCap/Controllers/RecorderController.swift
tags:
  - event-bus
  - race-condition
  - toctou
  - cursor-replay
  - subscribe-after-publish
  - daemon
  - supervisor
symptoms:
  - "`Supervisor.stop()` blocks for the full 30 s `stop_timeout` while `_exit_poll` already published `recording_finalized`."
  - "SwiftUI's `RecorderController` stays stuck in `.starting` after `recording.start` returns OK — the `recording_started` event never arrives over `/v0/events?since=cursor`."
  - "`pytest tests/daemon` is green but the production happy path stalls because tests deliver events AFTER the subscriber, masking the inverse production timing."
root_cause: >
  Phase 1 shipped the daemon EventBus with no replay buffer. Any subscriber
  that joined the bus after a producer published an event missed it entirely.
  Two real consumers materialized — `Supervisor.stop()` opening a late
  subscription to await `recording_finalized`, and SwiftUI subscribing to
  `/v0/events?since=cursor` after `recording.start` returned — and both
  hit the same shape: producer publishes during the subscriber's await gap,
  subscriber never sees the event, code blocks waiting for a notification
  that already passed.
---

## Recurring shape

Late-listener races have hit this codebase before in different costumes:

- **SIGINT/SIGTERM handler installed after a 30 s setup window** — handler
  was registered too late, so signals during the setup window were
  ignored. Four historical iterations of the same race shape. See
  [SIGINT handler timing](sigint-handler-timing-and-recording-stop-methods.md).
- **`Supervisor.stop()`'s late `bus.subscribe()`** — opens the subscription
  AFTER checking `is_alive()` returns True, and after publishing `terminate()`.
  If `_exit_poll` publishes `recording_finalized` in the await gap of
  `subscribe()`, the late subscription joins with `cursor_at_subscribe`
  already past the event. `_wait_on_subscription` then blocks until
  `stop_timeout` (30 s) before giving up.
- **SwiftUI `RecorderController.consumeDaemonEvents`** — subscribes to
  `/v0/events?since=snapshot.cursor` only after the `recording.start`
  HTTP response returns. The daemon's engine has already emitted
  `recording_started` by then; without replay, SwiftUI never observes the
  transition.

All three share the pattern: **the producer-consumer handshake assumes
strict before/after ordering, but the event loop's await points allow
the producer to slip ahead silently.**

## The fix: bounded replay buffer keyed by monotonic cursor

`EventBus` retains the last `REPLAY_BUFFER_SIZE = 256` stamped events in
a `collections.deque`. `subscribe(since: int | None = None)` extends the
prior live-only API:

- `since=None` → live-only (prior behavior).
- `since` strictly between the oldest retained cursor minus 1 and
  `current_cursor` → enqueue every retained event with `cursor > since`
  onto the new subscriber's queue before live delivery begins. The
  enqueue happens under the same `_lock` as `publish`, so a concurrent
  publish either lands in the ring before subscribe sees it (and gets
  replayed) or lands after subscribe registers (and gets delivered
  live) — never lost.
- `since > current_cursor` or `since < oldest_retained - 1` →
  `CursorUnknownError`, mapped at the HTTP boundary to the existing
  `cursor_unknown` 410 envelope.

`Supervisor.stop()` and `Supervisor.shutdown()` now capture
`pre_check_cursor = self._bus.current_cursor()` *before* their gate
(`is_alive()` / `terminate()`) and pass it to `subscribe(since=…)`.
Any lifecycle event published in the await gap lands in the ring and
is delivered to the late subscriber via replay — closing TKT-A.

The HTTP `/v0/events?since=N` route maps the bus's `CursorUnknownError`
to the typed 410, finally aligning the wire contract with the Phase 1
plan's stated intent. This also tightens the SwiftUI side: a
`?since=N` for N older than the retained window no longer silently
collapses to "live from now", so the controller knows to refetch a
fresh snapshot and resubscribe.

## Detection guide

When you see a daemon-side event that "should have arrived" never reaching
a subscriber:

1. Check whether the subscription was opened **after** the event was
   published. The cleanest tell: the event is in the bus's replay ring
   (inspectable via `bus._buffer`) but not in any subscriber queue.
2. If the subscription is the one inside `Supervisor.stop()` /
   `Supervisor.shutdown()`, confirm `subscribe(since=…)` is being
   called with a cursor captured before the gate, not `subscribe()`
   alone.
3. For HTTP `/v0/events?since=N` callers, capture the cursor returned
   by the corresponding `recording.start` response (or
   `session.snapshot`) and re-issue the subscribe with that exact
   value. A successful subscribe with the same cursor that previously
   returned 410 means the gap has aged out and the client should
   refetch.

## Why not swap the check/subscribe order?

The TKT-A ticket considered subscribing-before-checking as an alternative.
Rejected because the early-return paths (`no_recording`, `no_engine`,
dead-proc takeover) would each need to remove the subscription, leaking
on any forgotten exit. The replay-buffer fix keeps the control flow
intact and pushes the race coverage into the bus itself, where it is
testable in isolation.

## What stays deferred

- **Time-based eviction** — count-bounded ring is simpler, easier to test,
  and aligned with the cursor stamp semantics. Time eviction adds
  clock-skew failure modes for no real consumer benefit.
- **Coalescing strategies for high-frequency events** — the replay ring
  retains exactly what publish saw, in order. Coalesce-then-batch is a
  tunable for after the first consumer surfaces a need.
- **Stderr-channel decoupling (TKT-D approach 2: asyncio queue + drop
  policy)** — the F_SETPIPE_SZ widening at 1 MiB is the v1 safety net.
  If production traces ever show pump stalls beyond that ceiling,
  approach 2 becomes the next step.
