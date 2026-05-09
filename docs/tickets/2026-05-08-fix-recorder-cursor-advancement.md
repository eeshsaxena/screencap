---
title: "RecorderController reconnect: track lastDeliveredCursor across retries"
status: open
priority: medium
created: 2026-05-08
related_plans:
  - docs/plans/2026-05-08-001-feat-daemon-architecture-phase-1-plan.md
related_review_run: /tmp/compound-engineering/ce-code-review/20260508-210223-e214ede7/
---

# RecorderController reconnect: track lastDeliveredCursor across retries

## Problem

In [macos/ScreenCap/Controllers/RecorderController.swift:572](../../macos/ScreenCap/Controllers/RecorderController.swift), `consumeDaemonEvents` re-fetches `sessionSnapshot()` on every reconnect iteration and uses **the snapshot's cursor**, not the cursor of the last successfully delivered event. Events emitted between stream drop and the re-fetch snapshot are irretrievably lost.

The PR body's residual #17(a) characterizes this as "uses initial snapshot cursor for every retry." That framing is **wrong**: the cursor IS updated per re-fetch. The actual gap is that no code path saves `event.cursor` from received events anywhere, so the reconnect cursor is the server's current position at re-fetch time, not the position right after the last received event. A follow-up implementor reading only the PR body may write a "track cursor across retries" fix that doesn't address the dropped window around re-fetch.

## What's needed

1. Add `private var lastDeliveredCursor: Int? = nil` to RecorderController.
2. In `handleRecorderEvent` (or wherever events are consumed), update `lastDeliveredCursor = event.cursor` after each successful delivery.
3. In `consumeDaemonEvents`, on reconnect:
   ```swift
   let cursor = lastDeliveredCursor ?? snapshot.cursor
   try await daemon.subscribe(sinceCursor: cursor)
   ```
4. Update PR body's residual #17(a) framing to reflect the actual gap (events between stream drop and re-fetch snapshot, not "same cursor every retry").

## Why deferred from the inline review pass

Two reasons:
- The fix needs paired updates to PR body documentation (the residuals list). Inline fixers don't touch protected docs.
- Without daemon-side replay (see related ticket on EventBus replay), tracking `lastDeliveredCursor` only narrows the loss window from "since stream drop" to "since just-after-last-event" — better, but still loses events in the gap between drop and re-subscribe under daemon outage. Full fix requires the replay buffer too.

## Acceptance

- New SwiftUI test: subscribe → receive events → drop connection → reconnect → assert `since=<lastDeliveredCursor>` is sent.
- PR body's residuals list updated to reflect the real gap.

## References

- Tier-3 ce-code-review run artifact: `/tmp/compound-engineering/ce-code-review/20260508-210223-e214ede7/`
- Reviewers: correctness, reliability, previous-comments (cross-reviewer, confidence 100, P2)
- Related: replay-buffer ticket (also benefits the missed-`started`-event ticket).
