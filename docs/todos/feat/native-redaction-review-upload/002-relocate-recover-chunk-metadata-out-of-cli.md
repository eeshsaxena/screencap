---
title: "P2: Relocate _recover_chunk_metadata out of cli to break the cli↔review import cycle"
status: open
priority: medium
created: 2026-06-05
source: code-review (ce-code-review autofix, finding altitude #1)
related_plans:
  - docs/plans/2026-06-03-002-feat-native-redaction-review-upload-plan.md
related_review_run: /tmp/compound-engineering/ce-code-review/20260605-103121-cr/
---

# P2: cli↔review import cycle around shared recovery infrastructure

## Problem

`src/screencap/review.py` reaches back into `screencap.cli` for `_recover_chunk_metadata` (deferred import inside `_prepare_scrubbed_copy`), while `cli/__init__.py` imports `prepare_review_data` from `review.py` (deferred, inside `review_data_cmd`). That is a genuine circular dependency that only stays unbroken because both imports are function-local.

`_recover_chunk_metadata` is a ~150-line recovery routine (recording.db → per-chunk manifests + `events_*.jsonl`) with nothing CLI-specific in it — it takes a `Path` + a `Console` and writes files. It now has three call sites across two layers (upload in cli, `_prepare_scrubbed_copy` in review, plus its own test module). It sits in `cli/__init__.py` for historical reasons.

## What's needed

- Move `_recover_chunk_metadata` (and its helpers) to a non-CLI module — e.g. `screencap/recovery.py` (or alongside the export logic it already delegates to). Both `cli` and `review` then import *down* into it, eliminating the cycle and the deferred-import dance.
- Carry the `cloud_bound` REQUIRED-kwarg contract and the LOAD-BEARING ORDERING docstring with it intact.
- Update `tests/test_recover_chunk_metadata.py` imports.

## Why deferred

Pure structural refactor touching three modules + a test module; the deferred-import works correctly today, so it was out of scope for the feature PR. Worth landing deliberately to make the layering a structural guarantee rather than tribal knowledge.
</content>
