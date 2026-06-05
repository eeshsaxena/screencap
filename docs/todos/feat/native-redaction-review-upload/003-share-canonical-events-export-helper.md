---
title: "P3: Extract a shared ensure_canonical_events helper (review.py + upload loop)"
status: resolved
priority: low
created: 2026-06-05
resolved: 2026-06-05
source: code-review (ce-code-review autofix, finding reuse #1)
related_plans:
  - docs/plans/2026-06-03-002-feat-native-redaction-review-upload-plan.md
related_review_run: /tmp/compound-engineering/ce-code-review/20260605-103121-cr/
---

# P3: Duplicated canonical-events export gate

## Problem

`src/screencap/review.py` `_export_canonical_events` (lines ~77-105) is a near-verbatim copy of the upload command's inline export block (`src/screencap/cli/__init__.py` ~2858-2871): both gate on `any(d.glob("events_*.jsonl"))` (skip when chunked), skip when `events.jsonl` exists, then `build_export_metadata(exclude_moves=False)` + `export_recording(..., exclude_moves=False)`.

The two **must** stay byte-for-byte equivalent for "reviewed == uploaded" to hold (the review's scrubbed copy and the upload's scrubbed copy must contain the same event set). Nothing enforces that today — a future edit to one config could silently diverge them.

## What's needed

- Extract a shared `ensure_canonical_events(rec_dir, *, force=False)` helper (in `exporter.py`) and call it from both sites.
- Parameterize the upload block's extra behavior (`--force` re-export, `count == 0` warning) via the helper's return/flags.
- A test asserting both paths produce the identical event set for the same recording would lock the contract.

## Why deferred

The upload block has extra `--force`/warning behavior the helper must absorb; refactoring the upload loop is adjacent to already-shipped U2/U4 work. Low risk, low urgency — the configs match today.

## Resolution

Added `exporter.ensure_canonical_events(recording_dir, *, force=False)` — the single shared gate + config (skip when chunked, skip when `events.jsonl` present unless `force`, `exclude_moves=False`, `include_network` off), returning the export count or `None` when no export was needed. `review._export_canonical_events` now delegates to it; the upload loop calls it (keeping its console messaging + warn-on-failure). Tests: `test_ensure_canonical_events_gating` (all branches), plus the existing review/upload paths. Configs can no longer drift between the two paths.
