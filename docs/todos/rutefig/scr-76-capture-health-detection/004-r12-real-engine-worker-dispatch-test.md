---
title: "Add R12 real _engine-worker dispatch test (frozen-class regression)"
status: pending
priority: medium
created: 2026-05-29
source: code-review SCR-76 (testing + learnings findings, confidence 100 after cross-reviewer agreement)
related_plans:
  - docs/plans/2026-05-29-002-feat-scr-76-capture-health-detection-plan.md
related_review_run: /tmp/compound-engineering/ce-code-review/20260529-150208-deb0faed/
---

# Add R12 real _engine-worker dispatch test

## Problem

Plan requirement R12 (and U6 part (a)) called for the frozen-daemon dispatch path to be exercised end-to-end *"using the actual bundled entry-point semantics, so the broken-in-frozen-but-green-in-tests regression class cannot recur."* The shipped `tests/test_capture_health_frozen_dispatch.py` drives `_capture_health_tick` directly and adds AST/structural guards that no subprocess probe was reintroduced — but it never spawns the real `_engine-worker` Click entry point and runs `record()`'s supervisor loop across the dispatch boundary.

## Why it matters

A wiring regression in the `record()` supervisor block (e.g. a `task_by_name` key typo in the `_hc_alive` construction, a dropped `_health_prev_counts` update, or the health tick not being called from the loop) would pass every current test — exactly the "green-in-tests, broken-in-frozen" class R12 was written to close. The unit/AST coverage proves the *helpers* work and that no subprocess was added; it does not prove the watcher actually runs when the daemon spawns the engine.

## Suggested direction

Add one integration test that points `SCREENCAP_DAEMON_ENGINE_COMMAND` at the real `python -m screencap _engine-worker` entry, stubs `take_screenshot` → `None` and `get_active_window_data` → falsy, sets `CAPTURE_HEALTH_WINDOW_SECS`/`CAPTURE_HEALTH_DEBOUNCE_TICKS` low, runs the engine subprocess, and asserts a `capture_unhealthy` (or `permission_lost`) line appears on stderr within a bounded timeout. Mirror the `engine_command_factory` / `SCREENCAP_DAEMON_ENGINE_COMMAND` pattern in `tests/daemon/test_supervisor.py`.

## Verification

- The new test fails if the health check is removed from `record()`'s supervisor loop, or if the entry-point→`record()` wiring breaks — even when the helper unit tests still pass.
