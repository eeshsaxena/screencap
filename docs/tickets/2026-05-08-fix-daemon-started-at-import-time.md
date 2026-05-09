---
title: "_STARTED_AT module-level reflects import time, not daemon start time"
status: open
priority: medium
created: 2026-05-08
related_plans:
  - docs/plans/2026-05-08-001-feat-daemon-architecture-phase-1-plan.md
related_review_run: /tmp/compound-engineering/ce-code-review/20260508-210223-e214ede7/
---

# _STARTED_AT module-level reflects import time, not daemon start time

## Problem

[src/screencap/daemon/app.py:21](../../src/screencap/daemon/app.py) currently has `_STARTED_AT = time.time()` at module level. Two issues:

1. It reflects **import time**, not daemon start time. For the `screencap serve` path the difference is small (~ms), but in the frozen PyInstaller binary the import-graph cost is meaningful.
2. `app.py` is bundled in the frozen binary AND re-imported in the `_engine-worker` subprocess (because the spec's hidden imports include the daemon namespace). The engine worker's `_STARTED_AT` is the **child's** import time. If anything reads `_STARTED_AT` from a worker context, the value is wrong.

The visible symptom: `daemon.info.started_at` is currently correct only because the daemon process is the first to import `app.py`. If the import order ever changes, or if a future code path reads `_STARTED_AT` from a worker, the reported start time silently drifts.

## What's needed

The cleanest fix has design implications, which is why this is a ticket and not an inline edit:

**Approach 1.** Replace module-level `_STARTED_AT` with a `set_daemon_started_at(now: float)` helper, called once from `server.py` lifespan startup. The endpoint reads from a module-level variable that is `None` until set; raises if accessed before set. Catches incorrect reads from worker context loudly.

**Approach 2.** Move the `started_at` value into application state (`app.state.started_at`) instead of module-level. More idiomatic for ASGI; ties lifetime to the actual app instance, not the module.

Approach 2 is preferable but requires updating the endpoint to read from request state.

## Acceptance

- `daemon.info.started_at` reflects the actual `lifespan_startup` timestamp.
- A test that imports `app` from a non-daemon context and asserts the value is unset (Approach 1) or unavailable (Approach 2).

## References

- Tier-3 ce-code-review run artifact: `/tmp/compound-engineering/ce-code-review/20260508-210223-e214ede7/`
- Reviewer: kieran-python (confidence 75, P2)
