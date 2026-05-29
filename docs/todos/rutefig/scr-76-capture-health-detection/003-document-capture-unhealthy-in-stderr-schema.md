---
title: "Document capture_unhealthy in the stderr event schema doc (plan U5 deliverable)"
status: pending
priority: medium
created: 2026-05-29
source: code-review SCR-76 (api-contract finding, confidence 100)
related_plans:
  - docs/plans/2026-05-29-002-feat-scr-76-capture-health-detection-plan.md
related_review_run: /tmp/compound-engineering/ce-code-review/20260529-150208-deb0faed/
---

# Document capture_unhealthy in the stderr event schema doc

## Problem

The SCR-76 plan's U5 lists, as a deliverable, *"Modify `docs/research/2026-04-28-stderr-event-schema.md` (document the new event: fields, advisory semantics, no exit code)."* That file does not exist (verified: the path was never tracked on any ref — it is a dangling reference also cited by `_stderr_events.py`'s own module docstring).

**Validation note (code review follow-up):** the new `capture_unhealthy` event *is* documented in the `src/screencap/_stderr_events.py` module docstring — a thorough section was added covering the field set, the closed `reason` set, the `reader` enum, advisory (no-exit-code, never-terminal) semantics, and the no-runtime-text invariant — plus the plan, the engine constants, and the contract test. So the in-code contract exists; what's missing is specifically the dedicated `docs/research/...schema.md` spec file the plan named. Scope this todo accordingly (create the file and have it reference / not duplicate the docstring), and decide whether the dangling `docs/research/...` reference in `_stderr_events.py` should be repointed at the real schema-doc location.

## Why it matters

The plan explicitly named this doc the "cross-language contract." Without it, a future contributor on either side (Python emitter or Swift decoder) has no single source of truth for the `capture_unhealthy` field set, the `reason` closed set (`reader_stalled`, `listener_dead`), the `reader` enum (`screen`/`window`/`action`), the `elapsed` field, advisory (no-exit-code, never-terminal) semantics, and the invariant that `reason` must never carry runtime-derived text.

## Suggested direction

Create `docs/research/2026-04-28-stderr-event-schema.md` (or the project's current schema-doc location) with a `capture_unhealthy` section: `type`, `reason` (closed set), `reader` (enum), `elapsed` (float), `schema_version`, `ts`; advisory semantics; and the `permission_lost` reuse note for the TCC branch. Confirm whether the doc was intended to already exist on `main` (the plan said "Modify", implying it did) — if it was renamed/moved, point the schema reference at the live path.

## Verification

- The schema doc lists `capture_unhealthy` with the exact field/closed-set shape emitted by `_emit_capture_health_event`.
- The schema-doc path referenced from `_stderr_events.py` / the plan resolves to a real file.
