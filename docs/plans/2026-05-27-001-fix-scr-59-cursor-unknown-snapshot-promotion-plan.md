---
title: "fix(swift): promote .starting from snapshot on cursor_unknown (SCR-59)"
type: fix
status: completed
date: 2026-05-27
---

# fix(swift): promote .starting from snapshot on cursor_unknown (SCR-59)

## Summary

Add an `onPromoteFromSnapshot(startedAt:)` callback to `DaemonSession.EventStreamCallbacks`. The `cursor_unknown` catch branch in `consumeEventStream` invokes it with the in-hand snapshot; `RecorderController` handles it by promoting `.starting → .recording` via the existing `machine.observeActiveDaemonSession(startedAt:)` path — mirroring `syncDaemonSnapshot()`'s recovery posture. A red test on disk verifies the bug today and will turn green once the fix lands.

---

## Problem Frame

PR #180 ce-code-review flagged this at P2 / confidence 50 / `pre_existing: true` — the reviewer wasn't sure it was real. A targeted TDD pass on `2026-05-27` confirmed it via a deterministic XCTest reproduction in the existing `UnixHTTPTestServer` mock harness. When the daemon's replay buffer evicts the start cursor *and* the `started` event has already aged past the fresh snapshot cursor, `RecorderController` hangs in `.starting` indefinitely because:

1. The `cursor_unknown` catch in `consumeEventStream` clears `pendingStartCursor` and continues the loop with `snapshot.cursor` ([DaemonSessionService.swift:276-295](macos/Screencap/Controllers/DaemonSessionService.swift:276)).
2. The second subscribe at the fresh `snapshot.cursor` may not include `started` if it was already evicted — leaving the orchestrator's state machine waiting for an event that never arrives.
3. `RecordingStateMachine.handle(event:)` only transitions `.starting → .recording` on a `started` event ([RecordingStateMachine.swift:170-179](macos/Screencap/Controllers/RecordingStateMachine.swift:170)). The only other promotion path — `observeActiveDaemonSession(startedAt:)` — is wired exclusively into `syncDaemonSnapshot()` ([RecorderController.swift:190-208](macos/Screencap/Controllers/RecorderController.swift:190)), never from inside `consumeEventStream`.
4. `RecordingState.starting.isRecording == true` ([RecordingStateMachine.swift:18-23](macos/Screencap/Controllers/RecordingStateMachine.swift:18)), so the consume-loop's `while callbacks.isRecording()` predicate stays true; the loop never exits on its own.

The race condition is not deterministically reproducible against a live daemon (snapshot/replay timing), so the harness mock is the only realistic repro path.

---

## Requirements

- R1. After `cursor_unknown` evicts the start cursor and the daemon snapshot reports `is_recording=true && daemon_owned=true`, the controller transitions out of `.starting` without waiting for the (already-evicted) `started` event.
- R2. Existing recovery behavior is preserved: when `started` *is* still in the second-subscribe replay, the sibling test `testCursorUnknown410FromEventsStreamFallsBackThroughSnapshotRefetch` continues to pass.
- R3. The promotion is idempotent and never regresses a later state. `.starting`-only guard prevents clobbering `.recording` or `.stopping`.

---

## Scope Boundaries

- Refactoring the `cursor_unknown` retry-and-backoff loop itself (failure budget, exponential delay) — out of scope.
- Daemon-side replay-buffer changes (extending the window, persistence across restarts) — out of scope; tracked separately.
- The `lastDeliveredCursor` reconnect refactor at [docs/tickets/2026-05-08-fix-recorder-cursor-advancement.md](docs/tickets/2026-05-08-fix-recorder-cursor-advancement.md) — different reconnect path (stream drop, not 410); orthogonal.

### Deferred to Follow-Up Work

- Stale `macos/Screencap.xcodeproj/project.pbxproj` from fresh clones — newer Swift source files (e.g., `RecordingStateMachine.swift`, `DaemonSessionService.swift`) are not in the checked-in/regenerated pbxproj, so `xcodebuild test` from a clean clone fails to compile until `xcodegen generate` is run. The pbxproj is gitignored ([macos/.gitignore:2](macos/.gitignore)), so the fix is either a README note, a pre-build script, or a CI step. **Out of SCR-59 scope** — surface separately (likely a `docs/tickets/` entry).

---

## Context & Research

### Relevant Code and Patterns

- **Buggy site:** `cursor_unknown` catch at [DaemonSessionService.swift:276-295](macos/Screencap/Controllers/DaemonSessionService.swift:276) — clears `pendingStartCursor`, applies backoff, `continue`s the loop. Snapshot from the current iteration is in scope at this point (fetched at the loop top, line 226).
- **Callback shape to mirror:** `EventStreamCallbacks` struct at [DaemonSessionService.swift:61-67](macos/Screencap/Controllers/DaemonSessionService.swift:61). Existing fields use `@MainActor () -> Void` and similar — new field follows the same shape.
- **Orchestrator callback site:** `RecorderController.attachDaemonEventStream` at [RecorderController.swift:455-468](macos/Screencap/Controllers/RecorderController.swift:455). Callbacks are constructed inline as a `DaemonSession.EventStreamCallbacks(...)` literal.
- **Promotion mechanism to reuse:** `RecordingStateMachine.observeActiveDaemonSession(startedAt:)` at [RecordingStateMachine.swift:96-101](macos/Screencap/Controllers/RecordingStateMachine.swift:96) — already clears `pendingStartCursor`, sets `recordingStartedAt`, transitions to `.recording(elapsed:)`, and emits `.startElapsedTimer`. Exact semantics match what we need.
- **Reference invocation pattern:** [`syncDaemonSnapshot()` at RecorderController.swift:190-208](macos/Screencap/Controllers/RecorderController.swift:190) — same mechanism, different trigger. Pattern: `apply(machine.observeActiveDaemonSession(startedAt: startedAt))`.
- **Red test (already authored, on this branch, uncommitted):** [`testCursorUnknownEvictsStartedThenStuckInStartingWhenReplayHasAgedPast`](macos/ScreencapTests/RecorderControllerDaemonTests.swift) — verified to fail on `main` (`Expected .recording (via snapshot promotion); got starting`). Includes a `secondSubscribeCount` guard that confirms the recovery branch executed, so the assertion fails for the right reason (stuck state) rather than a harness timeout.
- **Sibling test (must stay green):** `testCursorUnknown410FromEventsStreamFallsBackThroughSnapshotRefetch` in the same file — its mock at line 306 emits `started` on the second subscribe, so it exercises the event-driven recovery path, not the new snapshot-promotion path. Unaffected by the fix.

### Institutional Learnings

- No directly applicable entries in `docs/solutions/` — the Swift daemon transport layer is recent enough that there are no captured learnings for this surface.

### External References

- [Linear SCR-59](https://linear.app/zk-email/issue/SCR-59/consumedaemonevents-can-stay-stuck-in-starting-after-cursor-unknown)
- [PR #180 ce-code-review](https://github.com/proteus-computer-use/screencap/pull/180) — correctness reviewer, run `20260515-172643-23d55cd7`

---

## Key Technical Decisions

- **Pass snapshot data via a NEW callback (`onPromoteFromSnapshot(startedAt:)`)** rather than (a) introducing a new `AttachOutcome` case, (b) synthesizing a fake `started` event onto `onEvent`, or (c) refetching the snapshot inside the catch. Rationale: minimal diff; mirrors existing callback architecture; reuses the snapshot already in scope at the catch site (no extra round-trip).
- **Promotion guard lives in the orchestrator, not the service.** `DaemonSessionService` has no view into `RecordingState` (and shouldn't — the refactor that extracted it deliberately layered the service below state). The orchestrator's handler guards on `case .starting` before applying the transition. Service-side, the trigger is gated on `snapshot.isRecording == true && snapshot.daemonOwned`.
- **Reuse `machine.observeActiveDaemonSession(startedAt:)` rather than add a new transition.** Its semantics already match: clear `pendingStartCursor`, set `recordingStartedAt`, transition to `.recording`, start the elapsed timer. Adding a parallel transition would duplicate behavior and create drift risk.

---

## Open Questions

### Resolved During Planning

- *Was the bug real?* — Yes, verified via the red test on disk (deterministic failure on `main`).
- *Should the snapshot fetch happen inline in the catch, or reuse the in-scope snapshot?* — Reuse the in-scope snapshot. It was fetched at the top of the same loop iteration; using it avoids an extra `sessionSnapshot()` round-trip and a second failure-mode to handle.
- *Should the orchestrator guard on `.starting`-only, or apply the promotion unconditionally?* — `.starting`-only. Unconditional promotion would clobber `.recording`'s `recordingStartedAt` (resetting elapsed) and could regress `.stopping` back to `.recording`. The guard keeps the change additive.

### Deferred to Implementation

- None — the implementation surface is small and fully scoped.

---

## Implementation Units

### U1. Add snapshot-promotion callback for cursor_unknown recovery

**Goal:** When the `cursor_unknown` recovery path finds an active daemon-owned snapshot, promote the orchestrator out of `.starting` from the snapshot's `startedAt` instead of waiting for an evicted `started` event.

**Requirements:** R1, R2, R3

**Dependencies:** None — the red test is already on disk and verifies the bug today.

**Files:**
- Modify: `macos/Screencap/Controllers/DaemonSessionService.swift` (add callback field to `EventStreamCallbacks`; invoke from `cursor_unknown` catch)
- Modify: `macos/Screencap/Controllers/RecorderController.swift` (add handler in `attachDaemonEventStream` callback bundle)
- Test (verifies green): `macos/ScreencapTests/RecorderControllerDaemonTests.swift::testCursorUnknownEvictsStartedThenStuckInStartingWhenReplayHasAgedPast`

**Approach:**
- Add `onPromoteFromSnapshot: @MainActor (Date) -> Void` to `DaemonSession.EventStreamCallbacks`.
- In `consumeEventStream`'s `cursor_unknown` catch, after `callbacks.clearPendingStartCursor()` and before the backoff sleep: gate on `snapshot.isRecording == true && snapshot.daemonOwned`; derive `startedAt = snapshot.startedAt.map(Date.init(timeIntervalSince1970:)) ?? Date()`; call `callbacks.onPromoteFromSnapshot(startedAt)`. Reuse the `snapshot` value already in scope at this point in the iteration.
- In `RecorderController.attachDaemonEventStream`'s callback bundle, add:
  ```
  onPromoteFromSnapshot: { [weak self] startedAt in
      guard let self else { return }
      if case .starting = self.state {
          self.apply(self.machine.observeActiveDaemonSession(startedAt: startedAt))
      }
  }
  ```
  *(directional sketch — not implementation spec)*
- Order of operations in the catch matters: log → clearPendingStartCursor → **invoke onPromoteFromSnapshot** → increment failure budget → backoff sleep → `continue`. Promotion happens *before* sleeping so the UI doesn't sit in `.starting` for an extra backoff window.

**Execution note:** TDD — the red test exists on disk and is failing on `main`. After implementing the fix, run only that test first and confirm it turns green before running the full suite. This is the verification of correctness.

**Patterns to follow:**
- `syncDaemonSnapshot()` at [RecorderController.swift:190-208](macos/Screencap/Controllers/RecorderController.swift:190) — same `apply(machine.observeActiveDaemonSession(startedAt:))` invocation, different trigger.
- `EventStreamCallbacks` field shapes at [DaemonSessionService.swift:62-66](macos/Screencap/Controllers/DaemonSessionService.swift:62) — `@MainActor` closures, single-purpose.

**Test scenarios:**
- **Happy path (already authored):** `testCursorUnknownEvictsStartedThenStuckInStartingWhenReplayHasAgedPast` — 410 cursor_unknown, second subscribe returns `subscribed` only (no `started`), snapshot reports active daemon-owned session with `started_at=10.0`. After 800ms wait, assert `state == .recording`. **Currently red on `main`; turns green with U1.**
- **Regression guard (already authored):** `testCursorUnknown410FromEventsStreamFallsBackThroughSnapshotRefetch` — same 410 trigger, but second subscribe emits `started`. With the fix, promotion may fire from the snapshot *or* the `started` event may arrive first — either path reaches `.recording`. Either is correct because the state machine's `started` handler is idempotent (`guard case .starting = state` at [RecordingStateMachine.swift:175](macos/Screencap/Controllers/RecordingStateMachine.swift:175) returns `[]` once already `.recording`). Test should continue to pass unchanged.
- **Edge case — promotion no-op when not `.starting`:** Add an assertion-level check or a small unit test confirming that a `cursor_unknown` later in a session (after `started` already promoted to `.recording`) does NOT regress state. Acceptable forms: (a) extend an existing reconnect test to inject a 410 mid-recording and assert `recordingStartedAt` is unchanged, or (b) add a state-machine-level test that asserts `observeActiveDaemonSession` semantics. Implementer's choice; document the choice in the PR.
- **Edge case — service-side guard:** Promotion is *not* triggered when `snapshot.daemonOwned == false`. This is structurally protected by the existing `if snapshot.isRecording == true, snapshot.daemonOwned == false { return .foreignClaimant }` check at [DaemonSessionService.swift:243-245](macos/Screencap/Controllers/DaemonSessionService.swift:243) — the loop returns before reaching the catch. No new test needed; covered by `testProbeDaemonSurfacesForeignRecordingAsLastError` (same shape, different trigger).

**Verification:**
- The red test `testCursorUnknownEvictsStartedThenStuckInStartingWhenReplayHasAgedPast` passes.
- The sibling test `testCursorUnknown410FromEventsStreamFallsBackThroughSnapshotRefetch` still passes.
- Full `RecorderControllerDaemonTests` suite passes.
- Full `RecordingStateMachineTests` suite passes.
- No new Swift concurrency / strict-concurrency warnings introduced.

---

## System-Wide Impact

- **Interaction graph:** Adds one callback edge from `DaemonSessionService.consumeEventStream` → `RecorderController.attachDaemonEventStream`'s callback bundle. No other consumers; `EventStreamCallbacks` is internal to the daemon transport.
- **Error propagation:** Unchanged. The `cursor_unknown` catch still increments the failure budget, applies backoff, and `continue`s. Promotion is additive — fires before the existing backoff, doesn't alter the failure-budget arithmetic.
- **State lifecycle risks:** Orchestrator's `.starting`-only guard contains the blast radius. If a future state is added to `RecordingState`, the guard is a single-line check that's easy to spot in review.
- **API surface parity:** `EventStreamCallbacks` is an internal struct in `DaemonSessionService.swift`. No external API change; no schema-version implications.
- **Integration coverage:** The existing red test covers the cross-layer scenario end-to-end (service catch → callback invocation → orchestrator handler → state machine transition → `@Published state` update observed by the test).
- **Unchanged invariants:** `RecordingStateMachine.observeActiveDaemonSession(startedAt:)` semantics, `syncDaemonSnapshot()` behavior, the `cursor_unknown` retry/backoff/failure-budget arithmetic, and the `started` event handler's idempotency — all unchanged.

---

## Risks & Dependencies

| Risk | Mitigation |
|------|------------|
| Promotion fires from a stale snapshot and clobbers a real `.recording` elapsed value | `.starting`-only guard in the orchestrator handler — promotion is impossible once state is `.recording` or beyond. |
| `started` event arrives via replay *after* promotion → state machine re-runs `.starting → .recording` | State machine's `started` handler at [RecordingStateMachine.swift:175](macos/Screencap/Controllers/RecordingStateMachine.swift:175) has `guard case .starting = state else { return [] }` — returns no-op effects once already `.recording`. Already covered by existing tests. |
| Snapshot's `started_at` differs from the daemon's actual record (e.g., engine-side clock drift) → UI elapsed displays slightly off | Negligible: same `started_at` value would be observed by `syncDaemonSnapshot()` at probe time, so the behavior is consistent across both promotion paths. Not worth complicating the fix. |

---

## Sources & References

- **Linear ticket:** [SCR-59](https://linear.app/zk-email/issue/SCR-59/consumedaemonevents-can-stay-stuck-in-starting-after-cursor-unknown) — opened from PR #180 ce-code-review, `correctness` reviewer P2 / confidence 50 / `pre_existing: true`.
- **Originating PR:** [proteus-computer-use/screencap#180](https://github.com/proteus-computer-use/screencap/pull/180)
- **Red-test verification branch:** `rutefig/scr-59-consumedaemonevents-can-stay-stuck-in-starting-after` — red test on disk, uncommitted. This plan is being written on the same branch.
- **Related but distinct:** [docs/tickets/2026-05-08-fix-recorder-cursor-advancement.md](docs/tickets/2026-05-08-fix-recorder-cursor-advancement.md) — `lastDeliveredCursor` reconnect refactor. Different reconnect path (stream drop, not 410). Not part of SCR-59.
