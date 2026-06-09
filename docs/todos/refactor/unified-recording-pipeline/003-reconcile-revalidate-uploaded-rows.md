---
title: "Terminal reconcile should re-validate already-UPLOADED rows, not only PENDING/FAILED"
status: pending
priority: medium
created: 2026-06-08
source: code-review of refactor/unified-recording-pipeline (concurrency/crash-safety angle)
related_plans:
  - docs/plans/2026-06-05-002-refactor-unified-recording-processing-pipeline-plan.md
---

# Terminal reconcile should re-validate already-UPLOADED rows

## Problem

`terminal_stage._reconcile_ledger_against_gcs` and `_mark_uploaded_chunks` only (re)probe chunks whose `upload_state` is PENDING/FAILED; an already-`UPLOADED` row is never re-stat'd. Combined with the live mirror writing `UPLOADED` from in-memory `EMITTED` (no fresh confirm at write time — `mark_uploaded`'s `on_confirm` hook is unused), a stale/false `UPLOADED` can never be downgraded. `finalize_gate_satisfied` then trusts the bare `UPLOADED` lifecycle, and the chunk becomes an eviction candidate.

## Severity

Defense-in-depth, not an active data-loss path: `retention.begin_eviction` does a **fresh** remote re-confirm immediately before any unlink (prevention rule 3), which is the real safety gate. The risk is a transient eventual-consistency false-positive at eviction time on a chunk whose `UPLOADED` was never independently confirmed.

## What to do

Either (a) have reconcile periodically re-validate a sample of UPLOADED rows against GCS and downgrade on a confirmed-absent, or (b) wire `mark_uploaded(on_confirm=...)` so the live mirror only writes UPLOADED after a fresh confirm. Keep the eviction-time re-confirm regardless (it is the load-bearing gate).
