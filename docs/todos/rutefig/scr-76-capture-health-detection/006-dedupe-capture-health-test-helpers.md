---
title: "Dedupe capture-health test helpers across the two new test files"
status: pending
priority: low
created: 2026-05-29
source: code-review SCR-76 (maintainability finding, confidence 75)
related_plans:
  - docs/plans/2026-05-29-002-feat-scr-76-capture-health-detection-plan.md
related_review_run: /tmp/compound-engineering/ce-code-review/20260529-150208-deb0faed/
---

# Dedupe capture-health test helpers across the two new test files

## Problem

`_capture_stderr`, `_referenced_names`, and `_ALIVE` are duplicated verbatim between `tests/test_capture_health.py` and `tests/test_capture_health_frozen_dispatch.py`.

## Why it matters

Two copies drift independently — a fix or extension to the helper (e.g. tightening `_capture_stderr` parsing) has to be made twice or the files silently diverge.

## Suggested direction

Extract the shared helpers into a `conftest.py` fixture or a `tests/helpers/capture_health.py` module and import from both files. Note the two `_capture_stderr` variants differ (one returns parsed dicts, the other parses lines) — if the signatures must stay distinct, rename the divergent one locally (e.g. `_raw_stderr`) rather than copying the whole body.

## Verification

- Both test files import the shared helper; `pytest tests/test_capture_health*.py` stays green.
