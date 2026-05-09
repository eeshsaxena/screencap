---
title: "Delete standalone _envelope() functions in errors.py — keep only exception classes"
status: open
priority: medium
created: 2026-05-08
related_plans:
  - docs/plans/2026-05-08-001-feat-daemon-architecture-phase-1-plan.md
related_review_run: /tmp/compound-engineering/ce-code-review/20260508-210223-e214ede7/
---

# Delete standalone _envelope() functions in errors.py — keep only exception classes

## Problem

[src/screencap/daemon/errors.py](../../src/screencap/daemon/errors.py) maintains every error type in **two parallel forms**:

1. A standalone `<error>_envelope(...)` function that returns the raw dict envelope.
2. A `DaemonAPIError` exception subclass with an `.envelope()` method.

Both encode the same logic. Different call sites use different paths — and they have **already diverged**: `cursor_unknown` had its HTTP status code split (410 in one track, 400 in the other) until the inline review pass aligned them. Future drift is the default outcome, not a possibility.

Pre-MCP this is a ticking maintenance trap — every new error type has to be added in two places, and every existing one is at risk of silent divergence.

## What's needed

1. Inventory both tracks. List every `_envelope()` function and every `DaemonAPIError` subclass.
2. Pick one (the exception class is preferable — it carries HTTP status, error code, and envelope shape together). Delete the other.
3. Update every call site that currently uses the deleted track. Most callers in `app.py` already use exception-raising; the standalone functions are typically used in test code and in a handful of error response builders.
4. Run the daemon test suite. Any test asserting envelope shape via the standalone path needs the call swapped.

## Why deferred from the inline review pass

The fixer pass ran out of safe scope. This is a ~10-30 file edit (depending on how many test cases reference the standalone form), and merging it into a fixer pass focused on local fixes risks landing a partial refactor. Better as its own focused PR.

## Acceptance

- Single track surviving in `errors.py`.
- `pytest tests/daemon -x` clean.
- `ruff check src/screencap/daemon/` clean (no unused imports left over).
- One commit, easy to review.

## References

- Tier-3 ce-code-review run artifact: `/tmp/compound-engineering/ce-code-review/20260508-210223-e214ede7/`
- Reviewers: api-contract, maintainability (cross-reviewer corroboration, confidence 100, P2)
- The cursor_unknown 410/400 divergence was a real instance of the failure mode this refactor prevents.
