---
title: "P1: concurrent cloud-bound recovery/export collides on a shared events*.jsonl.tmp"
status: open
priority: high
created: 2026-06-05
source: code-review (ce-code-review, finding adversarial #2 / F3; corroborated by correctness residual)
related_plans:
  - docs/plans/2026-06-03-002-feat-native-redaction-review-upload-plan.md
related_review_run: /tmp/compound-engineering/ce-code-review/20260605-112111-bcc9fab3/
---

# P1: shared `.tmp` path in recovery/export races between actors

## Problem

Both the upload loop and the review path run `_recover_chunk_metadata(d,
cloud_bound=True)` and a canonical events export against the **source** dir
before scrub (`src/screencap/cli/__init__.py` ~:2887; mirrored in
`review._prepare_scrubbed_copy`). The exporter writes through a **fixed-name**
temp file (`events.jsonl.tmp`, `src/screencap/exporter.py` ~:171–184).

This source-dir work happens **before** any scrub lock is taken. Two actors on
the same crash-recovered recording — two review windows, or review + upload —
can run recovery/export concurrently and collide on the shared `.tmp` path,
corrupting `events_NNNN.jsonl` (interleaved writes / one unlinking the other's
tmp).

## What's needed

- Run `_recover_chunk_metadata` + the canonical export under
  `recording_scrub_lock(name)` on the review path (and confirm the upload path's
  lock span covers line ~:2887, not just the scrub→ship section). See also
  follow-up **005** (the review read-back lock span) — both want the lock taken
  earlier and held longer.
- Give the exporter's temp file a process-unique suffix (PID + uuid) so
  concurrent writers can never collide even outside the lock.
- Add a test: concurrent recovery/export on an `events_*.jsonl`-bearing
  recording does not corrupt the event files.

## Why deferred

Touches the upload lock span and the exporter's temp-write contract; needs a
concurrency fixture. Confidence was moderate (race window, not yet reproduced),
so it was left for a deliberate fix rather than auto-applied.
