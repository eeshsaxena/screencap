---
title: "Cut the live finalize + manual upload over to run_terminal_stage"
status: pending
priority: high
created: 2026-06-08
source: ce-work execution + code-review of refactor/unified-recording-pipeline
related_plans:
  - docs/plans/2026-06-05-002-refactor-unified-recording-processing-pipeline-plan.md
---

# Cut the live finalize + manual upload over to run_terminal_stage

## Problem

U7 built `terminal_stage.run_terminal_stage` as the single disk-driven, idempotent, flock-guarded convergence point, but integrated it conservatively to avoid breaking the working live data-loss prevention:

- `collaborators.finalize_uploads` wraps its **existing** chunk_processor-based finalize in `terminal_lock`, but does not delegate the body to `run_terminal_stage`. The ledger is kept in sync via `chunk_processor._mirror_status_to_ledger`, not driven by the terminal stage.
- The `screencap upload` CLI path uploads via `upload_recording` directly (now guarded by `assert_promotable_to_cloud` + `terminal_lock`) rather than routing through `run_terminal_stage`'s reconcile/mark/sentinel gate.
- `daemon.supervisor.resume_terminal_stage` exists as a non-blocking resume seam but is **not auto-invoked** from `_handle_engine_exit` (deferred to avoid a half-wired auto-trigger).

So `run_terminal_stage` is fully built + tested but is not yet the primary live mechanism; the old finalize still drives live recordings.

## Why it matters

This is the last step to truly collapse the two paths into one (R1/R5). It is also a **hard prerequisite for enabling the masked-video flag** (see 002): with the flag ON, the live in-process upload path uploads rich video straight from the source dir, never through U6's masker — only the terminal stage masks. Until the live path delegates to the terminal stage, flipping the flag ON would ship unmasked video.

## What to do

1. Replace `_finalize_uploads_locked`'s body with a `run_terminal_stage` call (keeping the result-dict contract the post-stop messaging expects).
2. Route the `screencap upload` path through `run_terminal_stage`; move/retire the `assert_promotable_to_cloud` guard so the terminal stage reconciles-then-refuses-on-holes in one place.
3. Auto-invoke `resume_terminal_stage` on `recording_finalized`/daemon restart with appropriate scheduling.
4. Remove the now-redundant `collaborators` construction-time PUBLIC/`effective_upload`/`auto_delete` forks (U4b left them in place precisely because this cutover had not happened).
5. Re-validate the AE2/AE12 invariants end-to-end through the unified path.
