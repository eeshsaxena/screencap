---
title: "RecorderController state stuck in .starting because EventBus has no replay"
status: open
priority: high
created: 2026-05-08
related_plans:
  - docs/plans/2026-05-08-001-feat-daemon-architecture-phase-1-plan.md
related_review_run: /tmp/compound-engineering/ce-code-review/20260508-210223-e214ede7/
---

# RecorderController state stuck in .starting because EventBus has no replay

## Problem

In SwiftUI's `consumeDaemonEvents()` at [macos/ScreenCap/Controllers/RecorderController.swift:572](../../macos/ScreenCap/Controllers/RecorderController.swift), the client subscribes to the daemon event stream via `snapshot.cursor` after `recording.start` returns. But:

- `Supervisor.spawn()` already awaits `EVENT_STARTED` before responding to `recording.start`.
- Phase 1's `EventBus` has no replay buffer.

So by the time SwiftUI subscribes, the `started` event is permanently in the past and unreachable. `RecordingState` stays at `.starting`. `stop()` is then blocked because its guard requires `.recording`.

The unit test at [macos/ScreenCapTests/RecorderControllerDaemonTests.swift:34-38](../../macos/ScreenCapTests/RecorderControllerDaemonTests.swift) masks this bug because the mock daemon delivers `started` AFTER the subscribe — opposite of production ordering.

This is a real bug in the recording-start flow that nobody hit during development because the mock-daemon path doesn't reproduce production timing.

## Why deferred from the inline review pass

Two clean fixes, each is a contract change:

**Option A — Synthesize state from response.** After a successful `recording.start`, set `recordingStartedAt = Date()` and `state = .recording(elapsed: 0)` directly. Stop relying on the live `started` event for the `.starting → .recording` transition. Simple, but couples the client state machine to the assumption that `recording.start` returning means recording started.

**Option B — Add bounded replay to EventBus.** Daemon-side: keep the last N events (or last M seconds) in a circular buffer. When a subscriber asks `since=<cursor>`, replay any matching events from the buffer first. Cleaner long-term, helps the stop()-TOCTOU ticket too. But it's a daemon contract addition.

Option B is preferable but is a bigger change.

## Acceptance

- A SwiftUI integration test that reproduces production ordering: spawn the daemon (or mock at the byte level), call `recording.start`, observe the SwiftUI controller transitioning to `.recording`, **without** the mock injecting a post-subscribe `started`. Today's test masks the bug; a faithful test should fail before the fix lands.
- For Option B specifically: a daemon test that subscribes-after-emit returns the buffered event.

## References

- Tier-3 ce-code-review run artifact: `/tmp/compound-engineering/ce-code-review/20260508-210223-e214ede7/`
- Reviewer: correctness (confidence 75, P1)
- Related: stop() TOCTOU ticket — Option B benefits both.
