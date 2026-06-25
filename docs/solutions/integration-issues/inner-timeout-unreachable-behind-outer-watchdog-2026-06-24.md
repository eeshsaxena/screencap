---
title: "Inner blocking-wait timeout must sit under the outer watchdog, or its error path is structurally unreachable (SCR-165)"
date: 2026-06-24
category: integration-issues
module: terminal_stage
problem_type: integration_issue
component: macos-app-shell
root_cause: async_timing
resolution_type: code_fix
severity: low
related_components:
  - terminal-stage
  - cli
  - upload-pipeline
tags:
  - macos
  - cross-language
  - subprocess
  - timeout
  - watchdog
  - flock
  - async-timing
  - terminal-stage
---

# Inner blocking-wait timeout must sit under the outer watchdog, or its error path is structurally unreachable (SCR-165)

## Problem

`screencap upload` acquires a per-recording terminal-stage advisory flock in blocking-with-timeout mode (default 600s), and `terminal_lock` completes its blocking poll *before* emitting any stderr lifecycle event. The Swift `UploadController` arms a 120s inactivity watchdog that only resets on a parsed event — so on a contended lock the child stays silent, the watchdog fires at 120s (~480s before the child would raise `TerminalStageBusy`), SIGTERMs it, and the user sees a hard `.failed("upload timed out")` for what was actually a transient, retryable lock handoff.

## Symptoms

- Interactive uploads from the macOS app fail with `.failed("upload timed out")` whenever another process (a live finalize, a daemon resume, or a concurrent upload) holds the recording's terminal-stage lock.
- The failure surfaces at ~120s, well before the child has done any real work.
- SCR-158's dedicated retryable state — `.busy("Upload already in progress…")` — never appears for the interactive path, even though the underlying condition is exactly the one it was built for.
- No `upload_busy` stderr event is ever emitted in this scenario; the child is killed mid-wait, having emitted nothing.
- The same contended-lock condition behaves correctly from the CLI/agent path (it waits and converges), so the bug looks app-specific.

## What Didn't Work

SCR-158 *looked* complete: it added the `upload_busy` stderr event on the Python side and the retryable `.busy` state on the Swift side, with tests on both ends. The gap was that each layer's busy test injected the trigger **directly** rather than exercising the cross-layer timing:

- The Python test raised `TerminalStageBusy` from a mock (`run_terminal_stage` patched to immediately raise), so the `upload_busy` event was emitted instantly.
- The Swift test (`testUploadBusyEventTransitionsToBusyNotFailed`) emitted a synthetic `upload_busy` line via a `FakeUploadService`, so the `.busy` transition was reached without any wait.

Neither test could see the real relationship: a **600s** inner blocking lock-wait sitting silently behind a **120s** outer event-reset watchdog. Because `terminal_lock` does its entire blocking poll before emitting *any* event, the watchdog always preempted the inner timeout. The `.busy` path was structurally unreachable for the interactive spawn path — the bug lived entirely in the untested gap *between* the two correctly-built halves.

## Solution

Add a `--lock-timeout` flag to `screencap upload`, thread it into `run_terminal_stage(lock_timeout=...)`, and have the interactive Swift spawn pass a short value (30s) that fits under the 120s watchdog. The CLI/agent default stays 600s.

**Python — new flag threaded into the terminal stage** (`src/screencap/cli/__init__.py`):

```python
@click.option("--lock-timeout", type=click.FloatRange(min=0), default=600.0,
              help="Seconds to wait for a contended per-recording terminal lock "
                   "before reporting it busy (default: 600). The interactive app "
                   "passes a short value (well under its 120s watchdog) so a "
                   "contended lock surfaces as retryable rather than timing out.")
def upload(names, all_recordings, dry_run, force, jobs, no_delete, lock_timeout):
    ...
    result = run_terminal_stage(
        d,
        ...
        lock_timeout=lock_timeout,   # SCR-165
        force_destination=Destination.CLOUD,
        ...
    )
```

`run_terminal_stage` already accepted the bound and forwards it to the lock:

```python
def run_terminal_stage(..., lock_timeout: float = _DEFAULT_LOCK_TIMEOUT, ...):
    ...
    with terminal_lock(name, non_blocking=non_blocking, timeout=lock_timeout):
```

**Swift — interactive spawn passes a short timeout via a testable argv helper** (`macos/ScreenCap/Controllers/UploadController.swift`):

```swift
final class LiveUploadService: UploadService {
    /// Deliberately well under the UploadController 120s inactivityTimeoutSeconds
    /// watchdog so a contended lock raises TerminalStageBusy fast → emits
    /// upload_busy → renders as a retryable .busy.
    static let interactiveLockTimeoutSeconds = 30

    /// Extracted so the --lock-timeout injection is unit-testable (the
    /// UploadService seam otherwise hides argv from FakeUploadService).
    static func uploadArgs(name: String) -> [String] {
        ["upload", "--lock-timeout", String(interactiveLockTimeoutSeconds), "--", name]
    }

    func start(name: String, ...) throws -> SpawnedProcessHandle {
        try CLIClient.spawn(args: Self.uploadArgs(name: name), ...)
    }
}
```

Before, `start` spawned `["upload", "--", name]`; the only contention bound was the implicit 600s default. After, a contended lock raises `TerminalStageBusy` at 30s, the child emits `upload_busy`, and the controller renders `.busy` — comfortably before the 120s watchdog can fire.

## Why This Works

**Root cause:** an inner *silent blocking wait* (the 600s lock poll, which emits nothing until it resolves) was nested inside an outer *event-reset watchdog* (the 120s inactivity timer, which only survives if a parsed event arrives). With no event during the wait, the outer watchdog always reached its bound first and preempted the inner timeout — so the inner timeout's well-designed outcome (`TerminalStageBusy` → `upload_busy` → `.busy`) could never be observed. Shrinking the inner bound to 30s guarantees the inner timeout resolves and emits *before* the outer watchdog fires.

**Transferable principle:** whenever a silent, blocking inner operation runs behind an outer watchdog that only resets on observable progress, the inner operation's own timeout must be strictly less than the outer watchdog — with enough margin to cover spawn + startup + event-delivery latency. Otherwise the watchdog preempts the inner path and its entire error-handling branch becomes dead code. The chosen 30s gives a 90s margin under the 120s watchdog (ample for child spawn + Python import + event delivery) while still waiting out a brief lock handoff. The CLI/agent default stays 600s because there the loser *wants* to wait for the winner and converge idempotently over its committed ledger — no shorter wait would converge over a finalize that holds the lock across its whole critical section (recovery, scrub, upload, sentinel, retention) anyway. A bounded-retry policy was considered and rejected as redundant: blocking-with-timeout already polls every 0.1s.

## Amendment — the cliff was moved, not removed (SCR-175)

The margin analysis above (`lock_wait + spawn + import < watchdog`) is **incomplete**: it omits the prep phase *between* the lock wait and the first `upload_started` event. `run_terminal_stage` does its entire reconcile (`_reconcile_ledger_against_gcs` + `_revalidate_uploaded_chunks`, a `request_signed_urls` round-trip per not-yet-uploaded chunk) and the scrub/mask `produce()` **silently**, before `upload.py` emits `upload_started`. So the 30s lock wait, the reconcile, and the scrub all draw on the **same single 120s budget**. A lock contended-then-released at ~29s leaves only ~90s for an unbounded silent converge — a large recording can re-trip the watchdog as `.failed("upload timed out")`, the very symptom SCR-165 set out to remove, ~30s earlier on the clock. Shortening the *one* inner bound moved the cliff; it didn't remove it.

**Corrected invariant:** `lock_wait + silent_prep < watchdog` is not statically guaranteeable — `silent_prep` (scrub of an arbitrarily large recording) is unbounded. The durable fix is therefore not a tighter bound but **making the prep phase non-silent**: the terminal stage now fires an `on_progress(phase)` callback at the lock-acquired boundary (`"locked"`), per reconcile probe (`"reconcile"`), and right before `produce()` (`"scrub"`); the interactive `screencap upload` wires it to a lightweight `upload_preparing` stderr event. Because `UploadController.handleLine` resets the watchdog on *any* parsed event, each ping makes the inactivity watchdog measure genuine inactivity rather than total prep time — the lock wait no longer shares a budget with the converge, and a many-chunk reconcile is no longer one unbounded silent span.

**Sharper transferable principle:** an *inactivity* watchdog backstops a wedged child; it must therefore see a heartbeat on real progress, not just at the final outcome. Auditing "every blocking phase before the first event" (the rule below) is only half the job — for phases whose duration scales with input (a per-chunk reconcile, a whole-recording scrub) there is no safe static bound, so the phase must **emit progress**, not merely *fit under* the bound. Residual: a single never-scrubbed `produce()` exceeding 120s would still need progress emitted from *inside* the scrub seam — out of scope here (the interactive path reuses an already-scrubbed copy in the common case), tracked as a follow-up if it proves material.

## Prevention

1. **Pin the timing invariant in a test, not just the wiring.** The Swift test asserts both the argv injection and the structural relationship the fix depends on:

   ```swift
   func testInteractiveUploadPassesShortLockTimeoutUnderWatchdog() {
       XCTAssertEqual(
           LiveUploadService.uploadArgs(name: "rec-001"),
           ["upload", "--lock-timeout", "30", "--", "rec-001"]
       )
       // The whole point of SCR-165: the lock timeout must be strictly under
       // the default watchdog, or the busy event can't surface first.
       XCTAssertLessThan(
           Double(LiveUploadService.interactiveLockTimeoutSeconds),
           120,
           "interactive lock timeout must stay under the 120s inactivity watchdog"
       )
   }
   ```

2. **Cover the flag threading on the Python side too**, so the CLI default can't silently regress (`--lock-timeout 30` → `lock_timeout=30.0`; no flag → `lock_timeout=600.0`).

3. **General rule for any spawn-and-watch path:** if a parent watches a child with an inactivity/liveness watchdog that resets on parsed events, audit every blocking phase the child can enter *before its first event* — each such phase's internal timeout must be provably less than the watchdog bound. Directly-injected unit tests on each side are not sufficient; add at least one test that pins the two bounds in relation to each other (e.g. `XCTAssertLessThan(innerTimeout, watchdog)`), since the failure mode lives in the gap between layers that single-layer tests never traverse.

## Related Issues

- PR: [proteus-computer-use/screencap#275](https://github.com/proteus-computer-use/screencap/pull/275) (SCR-165); parent SCR-158 (PR #259) added the `upload_busy` event + Swift `.busy` state that this fix made reachable for the interactive path.
- **SCR-175** (this doc's *Amendment* above) — the follow-up that closed the prep-phase gap SCR-165 left open: the `upload_preparing` heartbeat (`terminal_stage.run_terminal_stage(on_progress=...)` → CLI `upload_preparing` → Swift watchdog reset) so the silent reconcile + scrub stop sharing one budget with the lock wait.
- [macos-foundation-process-pipe-pitfalls.md](./macos-foundation-process-pipe-pitfalls.md) — the same Swift `Foundation.Process` ↔ Python CLI seam; pitfall #1 (undrained pipe deadlocks the child → no events → host assumes wedged) is the mechanical sibling of this logical silence. This doc is the Python-side complement: cap the child's blocking wait so it emits a typed retryable event before the Swift watchdog fires.
- [daemon-start-failure-typed-error-before-spawn-2026-06-08.md](./daemon-start-failure-typed-error-before-spawn-2026-06-08.md) — same principle of making a failure observable with the right typed signal before the consumer's timing assumptions kick in.
- [../design-patterns/fallback-path-recovery-and-exit-code-signaling-2026-06-15.md](../design-patterns/fallback-path-recovery-and-exit-code-signaling-2026-06-15.md) — a retryable condition must be distinguishable from a hard failure across the process boundary; SCR-165 is the timeout-domain instance.
- [../runtime-errors/eventbus-late-listener-replay-2026-05-12.md](../runtime-errors/eventbus-late-listener-replay-2026-05-12.md) — sibling cross-layer async-timing bug where a Swift controller hangs on a Python-side event that never arrives in time.
