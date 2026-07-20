---
title: "review-data envelope timing fields are nullable — Swift consumers must not gate readiness on them"
date: 2026-06-01
category: integration-issues
module: macos-app-shell
problem_type: integration_issue
component: tooling
symptoms:
  - "A playable recording with no action events opens the review window to 'Couldn't prepare this recording' and never shows the video"
  - "review-data --json returns ok:true with started_at / duration_seconds as JSON null, but the Swift window treats it as a preparation failure"
root_cause: logic_error
resolution_type: code_fix
severity: medium
related_components:
  - tooling
tags:
  - macos
  - swiftui
  - review-window
  - review-data
  - json-contract
  - nullable
  - cross-language
  - scr-102
---

# review-data envelope timing fields are nullable — Swift consumers must not gate readiness on them

## Problem

The `screencap review-data --json <name>` envelope serializes `started_at` and `duration_seconds` as JSON `null` for any **playable** recording that has no action events. The Swift review window's `loadReviewData()` guard required both to be non-nil, so it dropped those recordings into `.failed` and the video never rendered (SCR-102, fixed in PR #202).

## Symptoms

- A playable recording with no action events opens the review window to "Couldn't prepare this recording" and never shows the video.
- `review-data --json` returns `ok: true` with `started_at` / `duration_seconds` as JSON `null`, but the Swift window treats it as a preparation failure.

## What Didn't Work

There was no failed-fix detour — the guard condition was the proximate cause and the trace was direct. The trap worth recording is the *assumption*: the upstream upload-review-screen plan (`docs/plans/2026-05-27-002-feat-upload-review-screen-plan.md`) documents the response envelope as `started_at: <float>` / `duration_seconds: <float>` (non-nullable). That stale spec is what makes a `guard let startedAt = …, let duration = …` look correct. It contradicts what the Python half (PR #199, U4) actually emits.

## Solution

Python emitter (`src/screencap/review.py`) legitimately returns `null` timing on a playable recording — `catalog._read_recording_meta` returns `None` on a DB read failure, a missing timestamp, **or a recording with no action events**:

```python
# review.py — ok: true even when timing is null
started_at: float | None = None
duration_seconds: float | None = None
if db_path is not None:
    started_at, duration_seconds, timing_error = _read_recording_meta(db_path)
return {"ok": True, ..., "started_at": started_at, "duration_seconds": duration_seconds}
```

Swift consumer (`macos/Screencap/Views/Review/ReviewWindowViewModel.swift`) — drop the timing fields from the guard and fall back to safe defaults:

```swift
// Before — null in either field → .failed, video never shows
guard envelope.ok,
      let videoPath = envelope.videoPath,
      let eventsPath = envelope.eventsPath,
      let startedAt = envelope.startedAt,
      let duration = envelope.durationSeconds
else { state = .failed(...) ; return }

// After — readiness gates only on ok + paths; timing falls back
guard envelope.ok,
      let videoPath = envelope.videoPath,
      let eventsPath = envelope.eventsPath
else { state = .failed(message: envelope.error ?? "Failed to prepare recording.", retryData: nil); return }
let data = ReviewData(
    videoURL: URL(fileURLWithPath: videoPath),
    eventsURL: URL(fileURLWithPath: eventsPath),
    startedAt: envelope.startedAt ?? 0,
    durationSeconds: envelope.durationSeconds ?? 0
)
state = .ready(data)
```

## Why This Works

`null` timing means "no action events," which means there are no timeline markers to place anyway, so a fallback origin of `0` and an unknown-duration of `0` lose nothing. The panes already handle `0`: `TimelinePane` short-circuits every math path on `guard durationSeconds > 0`, rendering an empty/disabled timeline, and `TimelineEventParser` only subtracts the origin from event timestamps that don't exist in this case. `.failed` stays reserved for genuine `ok: false` / missing-path envelopes (the R9 "can't process this video" error), which is the distinction the envelope's `ok` discriminator exists to carry.

## Prevention

- **Decode optional-in-the-contract fields as optional, and never let them gate readiness.** A nullable metadata field that has a sensible default is not a reason to fail. Reserve the failure path for the envelope's explicit error discriminator (`ok: false`).
- **Trust the emitter + its pinning test over a prose spec.** The Python side pins this with `test_nullable_metadata_serialized_as_json_null` (PR #199). When a plan doc and a tested emitter disagree, the test wins — the plan's `started_at: <float>` line is stale.
- **Regression test (added):** `ReviewWindowViewModelTests.testOkTrueWithNullTimingMetadataLandsOnReady` — `ok: true` + valid paths + nil timing must land on `.ready` with `startedAt == 0`, `durationSeconds == 0`. It fails on the old guard. The sibling `testOkTrueWithNilVideoPathLandsOnGenericFailure` keeps the path-guard honest, so the two together pin "fail on missing paths, not on missing timing."
- **Next consumer heads-up:** future U6/U8 work against this envelope should treat `started_at` / `duration_seconds` as nullable regardless of the plan doc's wording.

## Related Issues

- SCR-102 (this fix) — PR #202. Python half — PR #199 (SCR-97 / U4).
- [docs/solutions/ui-bugs/swiftui-windowgroup-vs-window-singleton-scene-2026-05-18.md](../ui-bugs/swiftui-windowgroup-vs-window-singleton-scene-2026-05-18.md) — same review-window subsystem.
- [docs/solutions/integration-issues/macos-foundation-process-pipe-pitfalls.md](./macos-foundation-process-pipe-pitfalls.md) — other Python↔Swift CLI-bridge gotchas.
