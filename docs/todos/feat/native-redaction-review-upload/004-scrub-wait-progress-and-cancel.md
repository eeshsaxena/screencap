---
title: "P3: 600s review-data scrub wait has no progress signal or cancel button"
status: resolved
priority: low
created: 2026-06-05
resolved: 2026-06-05
source: code-review (ce-code-review autofix, reliability RR-1 / swift-ios residual / learnings)
related_plans:
  - docs/plans/2026-06-03-002-feat-native-redaction-review-upload-plan.md
related_review_run: /tmp/compound-engineering/ce-code-review/20260605-103121-cr/
---

# P3: Indeterminate, uncancellable scrub wait in the review window

## Problem

`review-data` now runs the NER scrub before returning, and the SwiftUI `LiveReviewDataLoader` timeout was raised to a fixed 600s. On a large recording the operator can sit on the "Preparing what will upload…" spinner for up to 10 minutes with:
- no progress indication (the scrubber exposes no progress callback, so the spinner is indeterminate — accepted in the plan), and
- no cancel button during preparation (only after preparation, at the Upload/Cancel stage).

A scrub that fails at 598s shows the spinner the whole time, then the failed state.

## What's needed (any subset)

1. Add a Cancel button to the `preparing` state that SIGTERMs the `review-data` subprocess and dismisses the window (the CLIClient already supports termination).
2. If/when the scrubber gains a progress callback, surface a determinate bar and stream progress over stderr events (the `_stderr_events.emit_event` channel already exists).
3. Consider a per-recording-size–scaled timeout instead of the flat 600s.

## Why deferred

UX refinement, not a correctness/safety issue. The plan explicitly accepted the indeterminate spinner ("absent a progress callback, U8 uses an indeterminate spinner with copy"). Worth a follow-up once real operators report waits on large recordings.

## Resolution

Fixed the actionable part (item 1): `CLIClient.runOneShot` (behind `runJSONRaw`, used by `review-data`) now wraps its exit/timeout race in `withTaskCancellationHandler` and SIGTERMs the child on Task cancellation (Sendable-safe via the captured pid). The preparing-state Cancel button calls `dismiss()`, which tears down the window and cancels the `.task { await loadReviewData() }` → the in-flight scrub is now terminated immediately instead of running orphaned up to the 600s ceiling.

Items 2 (determinate progress bar) and 3 (size-scaled timeout) remain genuine future enhancements but are **blocked on a scrubber progress callback that does not exist** — the plan already chose the indeterminate spinner deliberately. With cancel-terminates-the-scrub in place, the orphaned-wait risk that motivated this todo is gone; the determinate-progress polish can wait for the scrubber to expose progress. Full macOS suite green after the change.
