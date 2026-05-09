---
title: "_terminate_pid_sync sends SIGTERM/SIGKILL to a stored PID without verifying identity"
status: open
priority: medium
created: 2026-05-09
related_plans:
  - docs/plans/2026-05-08-001-feat-daemon-architecture-phase-1-plan.md
related_review_run: /tmp/compound-engineering/ce-code-review/20260508-193011-a0d26cbf/
---

# `_terminate_pid_sync` sends SIGTERM/SIGKILL to a stored PID without verifying identity

## Problem

`Supervisor._terminate_pid_sync` (in [src/screencap/daemon/supervisor.py](../../src/screencap/daemon/supervisor.py)) sends `SIGTERM` and, on grace-window expiry, `SIGKILL` to whatever PID was recorded in the lock metadata. There is no verification that the PID still belongs to a `_engine-worker` process before the signal lands.

The window where this matters is daemon-startup orphan reconciliation: the daemon was killed (launchctl bootout, crash, machine sleep that lost the previous session) and on restart it reads `pidfile.read_lock_metadata()`, sees `claimant=daemon` with an `engine_pid` that may or may not still be the original engine process. macOS recycles PIDs aggressively. If the orphan engine has long since exited and the kernel has reused that PID for an unrelated process — even one owned by a different user, since `kill(2)` permission is enforced separately — the supervisor's reconcile path SIGTERMs the wrong process and synthesizes a `previous_session_force_terminated` event for a session that never happened.

In practice the window is bounded: orphan reconciliation only fires at daemon startup, the daemon is a per-user singleton, and most macOS sessions don't churn PIDs fast enough to hit a same-user collision in seconds. But the failure mode is silent (the wrong process just dies) and the recovery is expensive (the user has to figure out which app of theirs got killed).

Phase 2 doesn't amplify this — the orphan reconcile path stays the same shape regardless of whether the engine becomes in-process or stays subprocess. So this is a "real bug, low frequency, fix when convenient" item, not a Phase 2 blocker.

## What's needed

Verify process identity before signaling. Two cheap options:

1. **`psutil` cmdline match.** Read `psutil.Process(pid).cmdline()` and confirm `_engine-worker` appears in the argument list. If the process is gone or the cmdline doesn't match, log a warning and emit `previous_session_recovered` instead of `previous_session_force_terminated`. Skip the signal entirely.
2. **`create_time` match against the lockfile.** The pidfile already carries enough state to add a `create_time` value at lock-claim time (`psutil.Process().create_time()`). On reconcile, re-read the running process's `create_time` and bail if it differs. More precise than cmdline match — handles the case where someone else's process happens to share `_engine-worker` in their argv.

Recommended: do both. cmdline first as the cheap pre-check; create_time match as the authoritative gate. If either fails, treat the orphan as already-gone and clean up state without signaling.

Note: `pidfile.terminate_processes` (the existing CLI-side helper) does roughly this — `_is_screencap_process` at [src/screencap/pidfile.py:75](../../src/screencap/pidfile.py) checks cmdline for `screencap`. The daemon-side path should use the same primitive (or a refactored shared helper) rather than re-rolling the check.

## Acceptance

- A regression test simulates the PID-reuse case: pre-write a lockfile with a known-recycled PID (use a sleep subprocess and capture its PID, then exit it before reconciliation runs), confirm the supervisor does NOT signal the recycled PID and emits `previous_session_recovered`.
- A second test exercises the happy path (live orphan engine with matching cmdline), confirms SIGTERM still fires and `previous_session_force_terminated` is emitted.
- Code review of `_terminate_pid_sync` confirms no signal path bypasses the identity check.

## References

- Tier-2 ce-code-review run artifact: `/tmp/compound-engineering/ce-code-review/20260508-193011-a0d26cbf/`
- Reviewer: reliability (REL-02, confidence 85, P1) — downgraded to P2 here per triage discussion: failure window is bounded to daemon-startup orphan reconciliation, not amplified by Phase 2.
- Related primitive: `src/screencap/pidfile.py:_is_screencap_process` — the CLI-side cmdline check the daemon should reuse.
