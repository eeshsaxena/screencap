---
title: "Redundant captureAdvisory clear in .clearError — drop it or add a test seam"
status: pending
priority: low
created: 2026-05-29
source: code-review SCR-76 (testing + correctness + learnings; reclassified from safe_auto — untestable without a new seam)
related_plans:
  - docs/plans/2026-05-29-002-feat-scr-76-capture-health-detection-plan.md
related_review_run: /tmp/compound-engineering/ce-code-review/20260529-150208-deb0faed/
---

# Redundant captureAdvisory clear in .clearError — drop it or add a test seam

## Problem

`RecorderController.apply()`'s `.clearError` case sets `captureAdvisory = nil` (RecorderController.swift:465). Review wanted a test pinning that behavior, but it turns out **no reachable path fires `.clearError` while `captureAdvisory` is set**:

- `.clearError` is produced **only** by `RecordingStateMachine.enterStarting()` (RecordingStateMachine.swift:92), which `guard !state.isRecording`-no-ops while recording.
- `captureAdvisory` is set only during `.recording` (the `handleCaptureUnhealthy` guard) and is auto-cleared on the return to `.idle` via `state.didSet` (RecorderController.swift:93).

So by the time `enterStarting()` can emit `.clearError` (from a non-recording state), the advisory has already been nilled by the `.idle` chokepoint. The line-465 clear is **redundant defense-in-depth**.

## Why it matters

The redundant line is harmless but untestable through the existing public/test API, leaving a "missing test" smell that isn't actually coverable. A future reader may try to test it and hit the same wall.

## Suggested direction

Pick one:
1. **Drop the redundant clear** at RecorderController.swift:465 (rely on the `.idle` `didSet` chokepoint, which the existing comment already documents as covering every idle-transition path), and add a brief comment that advisory clearing is owned by `state.didSet`.
2. **Keep it as belt-and-suspenders** and add a code comment noting it is redundant with the `.idle` chokepoint and intentionally retained — so it doesn't read as testable-but-untested.

A test is only warranted if a real seam is added that can fire `.clearError` while the advisory is live; that seam doesn't exist today and shouldn't be invented just to test a redundant line.

## Verification

- Either the redundant clear is removed (and `pytest`/Swift tests stay green), or a comment documents the redundancy. No contrived test seam added.
