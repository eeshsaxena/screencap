---
title: "refactor: Remove ClaimLock (SCR-53)"
type: refactor
status: active
date: 2026-05-18
---

# refactor: Remove ClaimLock (SCR-53)

## Summary

Delete the `ClaimLock` policy class — unreachable in production after Phase 2 made the daemon the sole engine spawner — and make `InheritLock` the only / default `LockPolicy`. The deletion is mostly mechanical: one class plus its imports in `recorder.py`, plus a `ClaimLock()` → `InheritLock()` swap in seven `tests/engine/` files. The `LockPolicy` Protocol, `InheritLock`, and the shared `_write_identity_files` helper all stay; the `pidfile.*` primitives stay (still used by `session.py`, `daemon/supervisor.py`, `daemon/app.py`, `cli/__init__.py`). Tracks [SCR-53](https://linear.app/zk-email/issue/SCR-53/engine-remove-or-test-claimlock-unreachable-in-practice-after-phase-2).

---

## Problem Frame

[src/screencap/engine/lock_policy.py:106-112](src/screencap/engine/lock_policy.py:106) carries a comment admitting `ClaimLock.claim` is "exercised only via the daemon's engine subprocess path, which runs with `InheritLock` in practice." After Phase 2 U1.6 (which removed the legacy in-process `screencap start` path — see [tests/conftest.py](tests/conftest.py) header), the daemon's `run_recording_worker` is the only call site of `start_recording()`, and it always passes `_lock_policy=InheritLock()` ([src/screencap/session.py:205](src/screencap/session.py:205)). The only thing currently keeping `ClaimLock` alive is the `recorder.py:636` default and a handful of `tests/engine/` files that wire it for parity. The class is dead weight: its only production-style invariants are also dead weight, and the misleading comment will rot further as the engine-topology spike approaches.

---

## Requirements

- R1. `ClaimLock` is deleted from `src/screencap/engine/lock_policy.py`.
- R2. `recorder.py`'s default for `_lock_policy` becomes `InheritLock()`; all production paths continue to work unchanged (no behavior change in daemon-driven recordings).
- R3. `tests/engine/` files that constructed `ClaimLock()` for parity / lifecycle / per-policy tests are updated to `InheritLock()` and continue to pass.
- R4. The `LockPolicy` Protocol and the `_write_identity_files` helper survive (Protocol kept for the seam; helper still shared with `InheritLock`).
- R5. `pidfile.*` primitives (`claim_lock`, `find_orphaned_processes`, `terminate_processes`, `write_pidfile`, `delete_pidfile`) remain untouched — they have non-`ClaimLock` callers in `session.py`, `daemon/supervisor.py`, `daemon/app.py`, `cli/__init__.py`.
- R6. Stale `ClaimLock` references in comments / docstrings inside `screen_recorder.py` and `lock_policy.py` are cleaned up so the module reads coherently with one impl.
- R7. The full `pytest tests/` suite passes after the change.

---

## Scope Boundaries

- Not changing `pidfile.py` or any of its primitives.
- Not changing daemon-side lock semantics (`daemon/supervisor.py`, `daemon/app.py` are untouched).
- Not refactoring the `LockPolicy` Protocol away — see Key Technical Decisions.
- Not renaming `tests/test_pidfile_mutex.py::TestClaimLock` — it tests the lowercase `pidfile.claim_lock` primitive, not the deleted policy class. Renaming is cosmetic and risks confusion; left for any future doc-pass.
- Not touching the engine-topology spike ([docs/tickets/2026-05-08-engine-topology-spike.md](docs/tickets/2026-05-08-engine-topology-spike.md)). If the spike collapses the policy seam, that work supersedes this; meanwhile this deletion stands on its own.

---

## Context & Research

### Relevant Code and Patterns

- [src/screencap/engine/lock_policy.py](src/screencap/engine/lock_policy.py) — the `ClaimLock` class (lines 82-142) plus its module docstring and the `LockPolicy` Protocol that survives.
- [src/screencap/recorder.py:608](src/screencap/recorder.py:608), [src/screencap/recorder.py:636](src/screencap/recorder.py:636) — import + default-policy site.
- [src/screencap/engine/screen_recorder.py:192-200](src/screencap/engine/screen_recorder.py:192), [404-408](src/screencap/engine/screen_recorder.py:404), [613-620](src/screencap/engine/screen_recorder.py:613), [804-810](src/screencap/engine/screen_recorder.py:804) — comment / docstring references that need updating to drop "ClaimLock | InheritLock" framing.
- [src/screencap/session.py:205](src/screencap/session.py:205) — the canonical `_lock_policy=InheritLock()` call site (unchanged; this plan adopts its pattern as the production default).
- [tests/engine/test_lock_policy.py](tests/engine/test_lock_policy.py) — comprehensive existing test file; the seven `ClaimLock`-specific tests get deleted, the `InheritLock` and shared-identity tests stay.
- [tests/engine/test_screen_recorder_parity.py](tests/engine/test_screen_recorder_parity.py), [test_screen_recorder_lifecycle.py](tests/engine/test_screen_recorder_lifecycle.py), [test_network_policy.py](tests/engine/test_network_policy.py), [test_permission_policy.py](tests/engine/test_permission_policy.py), [test_signal_policy.py](tests/engine/test_signal_policy.py), [test_disk_policy.py](tests/engine/test_disk_policy.py) — all wire `lock=ClaimLock()` once each; mechanical swap.
- [tests/test_recorder.py:60-80](tests/test_recorder.py:60) — `test_force_exit_releases_lock_policy` does AST introspection of `lock_policy.release()` (interface-level); survives the deletion unchanged.

### Institutional Learnings

- None in `docs/solutions/` directly touch `LockPolicy`. The Phase 2 daemon migration plan ([docs/plans/2026-05-08-002-feat-daemon-architecture-phase-2-plan.md](docs/plans/2026-05-08-002-feat-daemon-architecture-phase-2-plan.md)) is the policy origin and confirms the "daemon is sole engine spawner" invariant this plan relies on.

### External References

- None — pure local deletion.

---

## Key Technical Decisions

- **Keep `LockPolicy` Protocol despite single implementation.** `RecordingPolicies.lock: LockPolicy` is the production type annotation and removing the Protocol would force a `lock: InheritLock` concrete-type binding throughout. The engine-topology spike ([docs/tickets/2026-05-08-engine-topology-spike.md](docs/tickets/2026-05-08-engine-topology-spike.md)) may re-introduce a different lock policy (e.g., if Option A "in-process engine" wins, the daemon-thread variant needs different identity-write semantics). One unused-but-cheap Protocol is better insurance than collapsing now and re-adding later.
- **Keep `InheritLock()` as the `recorder.py` default rather than removing the default and making `_lock_policy` required.** Two reasons: (a) preserves the existing `start_recording` signature, so the few legacy / direct-call test sites in `tests/test_recorder.py` and `tests/test_recording_integration.py` keep working without per-site rewrites; (b) `InheritLock` is the only correct choice for any future direct call — `ClaimLock`-style claim-from-engine-subprocess would deadlock against the daemon's own pidfile claim ([src/screencap/daemon/supervisor.py:257](src/screencap/daemon/supervisor.py:257)).
- **Do not delete the `_write_identity_files` helper or fold it into `InheritLock` directly.** Module-level keeps the "identity is independent of who owns the lock" invariant readable, even with one impl. Tiny cost; future Protocol expansion stays cheap.
- **Do not rename `tests/test_pidfile_mutex.py::TestClaimLock`.** It tests the lowercase `pidfile.claim_lock` primitive, not the deleted policy class. Rename would be a pure-cosmetic cross-cutting churn; out of scope.

---

## Open Questions

### Resolved During Planning

- *Are `pidfile.find_orphaned_processes` and friends orphaned by this deletion?* No — `session.py`, `daemon/supervisor.py`, `daemon/app.py`, `cli/__init__.py` all consume them.
- *Do any docs or scripts reference `ClaimLock`?* No — grep across `docs/`, `scripts/`, `macos/` returns zero hits.
- *Should `tests/test_recorder.py::test_force_exit_releases_lock_policy` change?* No — it AST-checks for a `lock_policy.release()` call, agnostic of the policy class.

### Deferred to Implementation

- Whether to inline `LockPolicy` into `screen_recorder.py` (currently a forward-declared Protocol stub there at [screen_recorder.py:192-201](src/screencap/engine/screen_recorder.py:192) with the body in `lock_policy.py`). If the comment in `screen_recorder.py` becomes incoherent after the deletion, the implementer may collapse the stub. Low-stakes editorial call; resolve when touching the file.

---

## Implementation Units

### U1. Delete `ClaimLock` and update `recorder.py` default

**Goal:** Remove the `ClaimLock` class and its import from production code; `InheritLock` becomes the default policy in `start_recording`.

**Requirements:** R1, R2, R4, R5

**Dependencies:** None

**Files:**
- Modify: `src/screencap/engine/lock_policy.py`
- Modify: `src/screencap/recorder.py`

**Approach:**
- In `lock_policy.py`: delete the `ClaimLock` class (lines 82-142). Keep the module docstring but revise it to drop the "two implementations" framing and explain only `InheritLock`'s role. Keep `LockPolicy` Protocol, `InheritLock`, and `_write_identity_files`.
- In `recorder.py`: change line 608 from `from screencap.engine.lock_policy import ClaimLock` to `from screencap.engine.lock_policy import InheritLock`. Change line 636 from `lock = _lock_policy if _lock_policy is not None else ClaimLock()` to `lock = _lock_policy if _lock_policy is not None else InheritLock()`.
- Run `ruff check src/screencap/engine/` to catch any stray imports.

**Patterns to follow:**
- The `_write_identity_files` shared helper pattern (already in [lock_policy.py:29-59](src/screencap/engine/lock_policy.py:29)) stays as the model for module-level helpers used by a single Protocol implementation.

**Test scenarios:**
- Happy path: `from screencap.engine.lock_policy import InheritLock, LockPolicy` succeeds; `from screencap.engine.lock_policy import ClaimLock` raises `ImportError`.
- Happy path: `start_recording(...)` with no `_lock_policy` override constructs an `InheritLock` instance internally (verify via the existing `tests/engine/test_screen_recorder_parity.py` flow after its U2 swap).

**Verification:**
- `ruff check src/screencap/engine/` exits clean.
- `python -c "from screencap.engine.lock_policy import ClaimLock"` raises `ImportError`.

---

### U2. Update `tests/engine/` ClaimLock → InheritLock swaps

**Goal:** All test files that constructed `ClaimLock()` to wire a `RecordingPolicies` now construct `InheritLock()` instead.

**Requirements:** R3, R7

**Dependencies:** U1

**Files:**
- Modify: `tests/engine/test_screen_recorder_parity.py`
- Modify: `tests/engine/test_screen_recorder_lifecycle.py`
- Modify: `tests/engine/test_network_policy.py`
- Modify: `tests/engine/test_permission_policy.py`
- Modify: `tests/engine/test_signal_policy.py`
- Modify: `tests/engine/test_disk_policy.py`

**Approach:**
- In each file: change `from screencap.engine.lock_policy import ClaimLock` to `from screencap.engine.lock_policy import InheritLock` and replace every `ClaimLock()` construction with `InheritLock()`. The known call sites are:
  - `test_screen_recorder_parity.py:87, 131, 193, 232`
  - `test_screen_recorder_lifecycle.py:102, 118`
  - `test_network_policy.py:362, 394`
  - `test_permission_policy.py:239, 273`
  - `test_signal_policy.py:170, 198`
  - `test_disk_policy.py:309, 350`
- No assertion changes expected: these tests don't depend on lock-claim observable side effects — they wire a `RecordingPolicies` and assert on the policy under test (network / permission / signal / disk / parity output). With `InheritLock` the lock-related calls become no-ops, which is exactly the production daemon-subprocess behavior the tests were already drifting toward.

**Patterns to follow:**
- The existing `test_screen_recorder_parity.py:131` style — `RecordingPolicies(signal=..., lock=ClaimLock(), menubar=...)` — is the canonical wiring shape; the swap is a one-token edit per call site.

**Test scenarios:**
- Happy path: each test file's existing assertions continue to pass with `InheritLock()` substituted.
- Verification: `pytest tests/engine/test_screen_recorder_parity.py tests/engine/test_screen_recorder_lifecycle.py tests/engine/test_network_policy.py tests/engine/test_permission_policy.py tests/engine/test_signal_policy.py tests/engine/test_disk_policy.py` runs green.

**Verification:**
- The six test files pass independently.
- No new test failures elsewhere (run full `pytest tests/` in U4).

---

### U3. Rewrite `tests/engine/test_lock_policy.py` to drop ClaimLock coverage

**Goal:** Remove `ClaimLock`-specific tests; preserve `InheritLock` and shared-identity coverage; update the module-level test to import only what survives.

**Requirements:** R3, R4, R7

**Dependencies:** U1

**Files:**
- Modify: `tests/engine/test_lock_policy.py`

**Approach:**
- Update the module docstring (lines 1-15) to describe only `InheritLock` and the shared `_write_identity_files` helper; drop the two-implementations framing.
- Update `test_lock_policy_module_exposes_protocol_and_impls` (line 23) to import `InheritLock, LockPolicy` only; rename to `test_lock_policy_module_exposes_protocol_and_inherit_lock` or similar.
- Delete the following tests:
  - `test_claim_lock_happy_path_runs_orphan_check_then_claims`
  - `test_claim_lock_orphans_without_force_clean_raises_systemexit_1`
  - `test_claim_lock_orphans_with_force_clean_terminates_and_proceeds`
  - `test_claim_lock_lock_contended_raises_systemexit_2`
  - `test_claim_lock_write_identity_writes_recording_id_and_intent`
  - `test_claim_lock_register_children_writes_pidfile`
  - `test_claim_lock_release_deletes_pidfile`
- Keep:
  - `test_inherit_lock_claim_and_release_are_noops` — the load-bearing test that worker recordings never touch the daemon's pidfile.
  - `test_inherit_lock_write_identity_writes_same_identity_files` — identity-file payload regression.
  - `test_write_identity_destination_cloud_only_when_keep_local_false` — already uses `ClaimLock().write_identity()` but is really testing the shared helper; rewrite to call `InheritLock().write_identity()` so the test remains agnostic of which policy invokes the helper.
- The `_assert_identity_files` helper and `_make_request` helper survive unchanged.

**Patterns to follow:**
- Keep the existing fixture / mock style — `unittest.mock.patch` on `screencap.pidfile.*` symbols is the established idiom; no need to switch to monkeypatch.

**Test scenarios:**
- Happy path: surviving tests pass — `test_inherit_lock_claim_and_release_are_noops`, `test_inherit_lock_write_identity_writes_same_identity_files`, `test_write_identity_destination_cloud_only_when_keep_local_false` (after rewrite to `InheritLock`).
- Edge case: the smoke-test confirms `from screencap.engine.lock_policy import ClaimLock` fails (covered in U1 verification; do not add a duplicate here).
- Verification: `pytest tests/engine/test_lock_policy.py` runs green and exposes no `ClaimLock` symbol.

**Verification:**
- `pytest tests/engine/test_lock_policy.py -v` reports the surviving test names and 0 failures.
- `grep ClaimLock tests/engine/test_lock_policy.py` returns no hits.

---

### U4. Clean up stale `ClaimLock` references in comments / docstrings

**Goal:** Eliminate misleading "ClaimLock | InheritLock" framing in code comments now that only `InheritLock` exists; confirm the change is grep-clean and the full suite passes.

**Requirements:** R6, R7

**Dependencies:** U1, U2, U3

**Files:**
- Modify: `src/screencap/engine/screen_recorder.py`

**Approach:**
- Update [screen_recorder.py:192-201](src/screencap/engine/screen_recorder.py:192) — the `LockPolicy` Protocol forward-declaration docstring. Drop the `ClaimLock | InheritLock` enumeration; describe only `InheritLock`'s role (write identity, no-op claim/register/release because the daemon owns the pidfile). If the standalone docstring no longer earns its keep, the implementer may collapse it inline at their discretion (see Open Questions → Deferred to Implementation).
- Update [screen_recorder.py:404-408](src/screencap/engine/screen_recorder.py:404) — the "Standalone CLI passes ClaimLock; session workers pass InheritLock" comment block. Rewrite to describe the post-Phase-2 reality: only `InheritLock` exists; `claim()` is a no-op because the daemon already owns the pidfile.
- Update [screen_recorder.py:613-614](src/screencap/engine/screen_recorder.py:613) — "both ClaimLock and InheritLock write them" — drop the "both" framing.
- Update [screen_recorder.py:804](src/screencap/engine/screen_recorder.py:804) — "ClaimLock writes the pidfile; InheritLock is a no-op" — rewrite to describe the InheritLock no-op behavior (the daemon supervisor handles pidfile lifecycle).
- Run a final `grep -rn "ClaimLock" src/ tests/` (excluding `tests/test_pidfile_mutex.py`'s lowercase-primitive references) to confirm the only remaining hits are intentional.

**Patterns to follow:**
- Keep comments terse and load-bearing — explain *why* the no-op is correct (daemon owns the pidfile), not *what* the no-op does (already obvious from the method body).

**Test scenarios:**
- Test expectation: none — this unit is pure comment / docstring editing with no observable behavior change. Coverage is provided by U2 / U3 + the full-suite run.
- Verification: `grep -rn "ClaimLock" src/screencap/ tests/engine/` returns zero hits. `grep -rn "ClaimLock" tests/` returns only `tests/test_pidfile_mutex.py` (which tests the lowercase primitive).

**Verification:**
- `pytest tests/` runs green end-to-end.
- `ruff check src/screencap/engine/` exits clean.
- `grep -rn "ClaimLock" src/` returns zero hits.

---

## System-Wide Impact

- **Interaction graph:** None. `start_recording` is called by exactly one production path (`run_recording_worker` in `session.py:175`) and that path already passes `InheritLock` explicitly — changing the default has no production-runtime effect.
- **Error propagation:** No change. `lock_policy.claim()`, `register_children()`, `release()` all become no-ops in the default path (already the case for the daemon-subprocess path). `SystemExit(1)` and `SystemExit(2)` paths from `ClaimLock.claim` are dead in production; deleting them removes a tested-but-unreachable branch.
- **State lifecycle risks:** None. The daemon supervisor ([daemon/supervisor.py:257](src/screencap/daemon/supervisor.py:257)) is the production owner of pidfile claim / release; nothing in this plan touches that. The previous `ClaimLock.release()` → `pidfile.delete_pidfile()` chain was effectively shadowed by the daemon's own cleanup; removing it leaves daemon-driven cleanup as the single source of truth.
- **API surface parity:** None — no public API touched. `start_recording`'s signature is unchanged; the `_lock_policy` keyword still accepts any `LockPolicy` implementation.
- **Integration coverage:** Covered by `tests/engine/test_screen_recorder_parity.py` (CLI-adapter vs direct `ScreenRecorder().run()` parity) after U2 swap, plus the four per-policy seam tests (`network`, `permission`, `signal`, `disk`).
- **Unchanged invariants:**
  - `pidfile.*` module (claim_lock, find_orphaned_processes, terminate_processes, write_pidfile, delete_pidfile, update_lock_metadata, etc.) — all kept, all still called by `session.py`, `daemon/supervisor.py`, `daemon/app.py`, `cli/__init__.py`.
  - The `LockPolicy` Protocol — kept as the seam for the engine-topology spike.
  - `RecordingPolicies.lock: LockPolicy` typing — unchanged.

---

## Risks & Dependencies

| Risk | Mitigation |
|------|------------|
| A hidden caller of `ClaimLock()` exists outside the test files I grepped (e.g., a vendored script, a notebook, or an out-of-tree consumer). | Final `grep -rn "ClaimLock"` in U4 catches anything in-tree. Out-of-tree consumers would have already broken at Phase 2's CLI-path closure — this plan adds no new surface. |
| Engine-topology spike resolves while this plan is in flight and wants the `LockPolicy` Protocol gone. | Low likelihood; the spike has no owner / date. If it lands first, this plan trivially adapts: delete the Protocol too in U1. If this plan lands first, the spike has cleaner ground to work from. |
| A `tests/engine/` test file relied on a `ClaimLock`-specific observable (e.g., the orphan-check side effect) that breaks after swap to `InheritLock`. | Local inspection of the six call sites shows none assert on `pidfile.*` mocks — they wire `lock=ClaimLock()` only to satisfy `RecordingPolicies` and test other policies. U4's full `pytest tests/` run is the safety net. |

---

## Documentation / Operational Notes

- No docs to update — `docs/` already contains zero `ClaimLock` references.
- No operational rollout — pure code deletion behind an already-collapsed CLI path.

---

## Sources & References

- **Linear issue:** [SCR-53](https://linear.app/zk-email/issue/SCR-53/engine-remove-or-test-claimlock-unreachable-in-practice-after-phase-2)
- **Originating PR review:** [PR #175 (Phase 2a daemon migration)](https://github.com/proteus-computer-use/screencap/pull/175), walk-through finding #29.
- **Phase 2 plan that closed the standalone CLI path:** [docs/plans/2026-05-08-002-feat-daemon-architecture-phase-2-plan.md](docs/plans/2026-05-08-002-feat-daemon-architecture-phase-2-plan.md) (U1.6).
- **Engine-topology spike (related, may obsolete this seam):** [docs/tickets/2026-05-08-engine-topology-spike.md](docs/tickets/2026-05-08-engine-topology-spike.md).
- Code: [src/screencap/engine/lock_policy.py](src/screencap/engine/lock_policy.py), [src/screencap/recorder.py:636](src/screencap/recorder.py:636), [src/screencap/session.py:205](src/screencap/session.py:205), [src/screencap/daemon/supervisor.py:257](src/screencap/daemon/supervisor.py:257).
