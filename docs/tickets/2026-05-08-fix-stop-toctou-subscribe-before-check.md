---
title: "Fix stop() TOCTOU between is_alive() and bus.subscribe()"
status: open
priority: high
created: 2026-05-08
related_plans:
  - docs/plans/2026-05-08-001-feat-daemon-architecture-phase-1-plan.md
related_review_run: /tmp/compound-engineering/ce-code-review/20260508-210223-e214ede7/
---

# Fix stop() TOCTOU between is_alive() and bus.subscribe()

## Problem

In `Supervisor.stop()` at [src/screencap/daemon/supervisor.py:302](../../src/screencap/daemon/supervisor.py), there's an `await` gap between the `is_alive()` check at line 290 and `bus.subscribe()` at line 302. The `_exit_poll` task can publish `EVENT_RECORDING_FINALIZED` in that window. `stop()` then subscribes after the event is already gone and blocks for the full 30s `stop_timeout` before returning a stale state.

Reproducible scenario: a graceful engine shutdown that races with an external `recording.stop` call. The engine self-exits at the moment the supervisor enters `stop()`, the poll task publishes finalized, and the external caller's stop subscription never sees it.

## Why deferred from the inline review pass

Surfacing the fix safely requires either:
- subscribing before the `is_alive()` check (changes early-exit semantics for the dead-engine branches at lines 264 and 290), or
- capturing a bus cursor before the check and re-subscribing with replay (Phase 1 EventBus has no replay, so this needs a small replay-buffer addition).

The first approach is simpler but flips the order of `_bus.subscribe()` / `_bus.remove()` for the no-recording / no-engine branches, which currently never subscribe. Done carelessly, this leaks subscriptions on the early-return paths.

## What's needed

1. Decide between the two approaches above. Replay-buffer is cleaner long-term and helps with finding #10 (RecorderController missed-`started`-event). Subscribe-then-recheck is local but fragile.
2. Implement, including the subscription cleanup on early-exit paths.
3. Add the test currently missing: a fake engine that self-exits the moment `stop()` is called. (Currently uncovered — see `tests/daemon/test_control_verbs.py:256` gap.)

## Acceptance

- New test that exercises the race deterministically (e.g. emit `recording_finalized` from a fake `_exit_poll` between `is_alive()` and `subscribe()`).
- `stop()` returns within 1s in this scenario, not 30s.
- `pytest tests/daemon -x` clean.

## References

- Tier-3 ce-code-review run artifact: `/tmp/compound-engineering/ce-code-review/20260508-210223-e214ede7/`
- Reviewer: reliability (confidence 100, P1)
