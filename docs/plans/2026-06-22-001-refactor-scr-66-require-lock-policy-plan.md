---
title: "refactor: Require explicit _lock_policy at start_recording (SCR-66)"
type: refactor
status: completed
date: 2026-06-22
---

# refactor: Require explicit _lock_policy at start_recording (SCR-66)

## Summary

Make `_lock_policy` a required keyword-only argument of `screencap.recorder.start_recording`, removing the `InheritLock()` hard-default at `src/screencap/recorder.py:643`. This lifts the daemon-only lock invariant — today documented only in a prose `# NOTE:` comment — into a `TypeError` at the call boundary. The sole production caller already passes the policy explicitly, so this is zero-impact for real recordings; the entire cost is updating ~30 direct test call sites. Call sites are migrated *before* the default is dropped, so the suite stays green at every commit.

---

## Problem Frame

After SCR-53 (PR #183) removed `ClaimLock`, `src/screencap/recorder.py:643` resolves `_lock_policy` to a default `InheritLock()` — a no-op `claim`/`register`/`release` that only writes per-recording identity files. `InheritLock` is *only* correct inside a daemon-spawned worker, because the daemon supervisor (`daemon/supervisor.py` → `pidfile.claim_lock`) owns the process-exclusive pidfile there. That invariant lives only in a prose comment at `recorder.py:636-642`; the code does not enforce it.

Any direct caller that omits `_lock_policy` (a test, an ad-hoc script, an MCP integration, or a future engine-topology variant) silently inherits the no-op lock and gets no process-exclusion. Two concurrent `start_recording('same-name')` calls then race the `capture_dir.exists() and any(capture_dir.iterdir())` check at `src/screencap/engine/screen_recorder.py:412-416`: both can pass, both `mkdir(exist_ok=True)`, both clobber `.recording_id`/`.recording_intent`, and both `Recorder` instances race `recording.db` and `chunk_0_video.mp4`. Pre-SCR-53, `ClaimLock` raised `LockContended` → `SystemExit(2)` here. No production caller bypasses the daemon today, so the regression surface is future contributors and the engine-topology spike — but the contract is unenforced.

---

## Requirements

- R1. Omitting `_lock_policy` at a direct `start_recording` call must fail loudly at the call boundary (a `TypeError`), not silently inherit a no-op lock.
- R2. All real recording behavior is unchanged: the sole production caller (`session.run_recording_worker`) and the daemon's pidfile-based exclusion are untouched.
- R3. The prose `# NOTE:` invariant at `recorder.py:636-642` is replaced by an enforced contract, and the comment is updated to describe enforcement rather than a request.
- R4. The change is robust to the still-open engine-topology spike (`docs/tickets/2026-05-08-engine-topology-spike.md`) — no coupling to the current subprocess topology or any daemon env-var convention.
- R5. The test suite is green at every commit (no red intermediate state), and a new test pins the invariant directly (not just per-call-site happy paths).

---

## Scope Boundaries

- **Not** making the other worker-mode seams (`_menubar_policy`, `_signal_policy`, `_permission_policy`, `_disk_policy`, `_network_policy`) required. Their defaults (`SpawnNewMenubar`, `ThreeTapSigint`, `MacOSTCC`, `MonitorAndStop`, network `Null`/`MitmProxyV15`) are safe, full-featured standalone-CLI defaults. `InheritLock` is the one default that is an unsafe no-op for non-daemon callers.
- **Not** adding real process-exclusion for direct callers. This change forces a *conscious* lock choice; it does not close the race at `screen_recorder.py:412-416`. Two callers that both explicitly pass `InheritLock()` concurrently still race. Real direct-caller exclusion (a genuine `LockPolicy` variant) is the engine-topology spike's territory, not this ticket's.
- **Not** Option B (the env-var daemon-context check from the ticket). Rejected: it couples to the current subprocess topology, and its `SCREENCAP_DAEMON_CONTEXT` marker would be stripped on the headless auto-spawn path by `cli/_autospawn.py` (which deliberately strips the `SCREENCAP_DAEMON_*` namespace), silently disabling the guard exactly where it would matter most.

### Deferred to Follow-Up Work

- Introduce a shared `all_noop_policies()` / no-op-policy test helper (the `RecordingPolicies` docstring at `engine/screen_recorder.py` already references one that does not exist): only worthwhile if a *second* policy seam is ever tightened. For this one-time change, inline explicit policies are convention-aligned and avoid premature abstraction.

---

## Context & Research

### Relevant Code and Patterns

- `src/screencap/recorder.py:562-680` — `start_recording`, the thin CLI adapter. Keyword-only seam block at `:587-594`; `_lock_policy` declared at `:590`; the prose NOTE at `:636-642`; the default resolution `lock = _lock_policy if _lock_policy is not None else InheritLock()` at `:643`; the local `InheritLock` import at `:608`.
- `src/screencap/engine/screen_recorder.py:250-264` — **`RecordingPolicies`, the precedent.** A frozen dataclass with **no field defaults** and a docstring that already states the SCR-66 thesis one layer down: *"No defaults — production must be explicit. Defaulting any field to Noop would silently disable lock/signal/privacy."* `start_recording`'s `_lock_policy` default is the single place that re-softens this back to implicit; Option A re-aligns the boundary.
- `src/screencap/engine/lock_policy.py` — the `LockPolicy` Protocol and `InheritLock` (no-op claim/register/release; writes identity files via `_write_identity_files`). `InheritLock()` is intentionally constructible with no args (pinned by `tests/test_lock_policy.py`).
- `src/screencap/session.py:206-238` — `run_recording_worker`, the **sole production caller**, which already passes `_lock_policy=InheritLock()` explicitly (`:236`). This is why dropping the default has zero production impact.
- `src/screencap/engine/screen_recorder.py:410-416` — `lock_policy.claim(...)` (no-op under `InheritLock`) immediately followed by the unguarded `capture_dir.exists() and any(capture_dir.iterdir())` race check. Confirms the race surface; out of scope to fix here.
- Error-surfacing convention in this layer: user-facing runtime errors use `console.print("[red]Error:[/red] ...")` + `SystemExit` (e.g. `screen_recorder.py:412-416`, `:423-425`). API-misuse (a missing required kwarg) is a *developer* error and correctly surfaces as a native `TypeError` at call binding — matching the `RecordingPolicies` no-defaults stance, not the user-facing path.
- Deferred-import discipline (CLAUDE.md): `start_recording` imports the engine + policy classes inside the function body to keep `screencap --help` fast. Preserve this.

### Institutional Learnings

- `docs/solutions/integration-issues/daemon-start-failure-typed-error-before-spawn-2026-06-08.md` — the team's enforcement idiom for the daemon start path: lift an implicit/async precondition into an explicit typed error at the right boundary, sequenced deliberately relative to `pidfile.claim_lock`. Reinforces "enforce the contract at the boundary," and its caveat — *verify the actual code flow before trusting a prose failure description* — was applied here (confirmed `InheritLock` is the silent default and `ClaimLock` is gone).
- `docs/solutions/runtime-errors/capture-health-nonscreen-attribution-terminal-stop-2026-06-01.md` — a defect survived because tests blessed a mapping *in isolation* and *none encoded the invariant*. Direct mandate for R5: add a test that pins the invariant (a direct call without `_lock_policy` raises), not just per-call-site happy paths that would stay green even if a no-op default were reintroduced.
- `docs/solutions/workflow-issues/privacy-guards-deselected-on-ci-silently-rot-2026-06-10.md` — guard tests only protect what actually runs; keep the new invariant test in a normal CI lane. Also names the exact antipattern here: *a default that couples two concerns where something else already enforces the invariant* — `InheritLock`'s no-op default conflates "daemon owns exclusion" with "this caller gets a safe default," while the supervisor pidfile is the real exclusion.
- No `docs/solutions/` doc covers the `LockPolicy` seam, `InheritLock`/`ClaimLock`, SCR-31/40/53, or the engine-topology spike. Worth a `/ce-compound` capture after this lands.

### External References

- None. Internal API-boundary change with strong local precedent (`RecordingPolicies`); no external research warranted.

---

## Key Technical Decisions

- **Option A (required keyword-only `_lock_policy`) over Option B (env-var check).** Type-level honesty, zero production impact (the sole prod caller already passes it), no daemon coupling, and robust to the open engine-topology spike. Option B is topology-coupled and its `SCREENCAP_DAEMON_*` marker is stripped on the headless auto-spawn path (`cli/_autospawn.py`), breaking the guard exactly where it matters. (User-confirmed.)
- **Raw native `TypeError` as the enforcement, not a wrapped `SystemExit` + `console.print`.** A missing required kwarg is API misuse by a developer, caught at call binding before the body runs (so no partial side effects). This mirrors `RecordingPolicies`' no-defaults stance; the `console.print`/`SystemExit` convention is reserved for user-facing runtime failures.
- **Only `_lock_policy` becomes required.** It is the one seam whose default silently disables a safety property; the others have safe standalone-CLI defaults. Making all seams required would be scope creep and would break every direct caller far more broadly.
- **Inline `_lock_policy=InheritLock()` at each call site, not a forwarding test wrapper.** Convention here is inline policy construction; a wrapper for a one-time seam tightening is premature abstraction (YAGNI). The shared-helper idea is deferred to follow-up.
- **Green-at-every-commit ordering.** Migrate call sites first (valid while the default still exists, since passing an explicit policy is always allowed), then drop the default (suite stays green because every caller already passes it).

---

## Open Questions

### Resolved During Planning

- Option A vs B vs defer → **Option A** (user-confirmed); the open engine-topology spike is not a blocker because Option A is robust to its outcome.
- Enforcement shape → native `TypeError` (no `SystemExit` wrapper).
- Call-site migration shape → inline explicit policy (no wrapper/helper).

### Deferred to Implementation

- Confirm the now-unused `InheritLock` import at `recorder.py:608` has no other reference in the file before removing it (the prose NOTE *mentions* `InheritLock` but is a comment, not a reference). `recorder.py` is in the `screencap` layer, not `engine/`, so the documented `ruff check src/screencap/engine/` lane will **not** flag a leftover unused import — verify by inspection.

---

## Implementation Units

### U1. Migrate all direct `start_recording` call sites to pass `_lock_policy` explicitly

**Goal:** Every direct caller of `start_recording` explicitly passes `_lock_policy=InheritLock()` so that dropping the default in U2 leaves the suite green. Behavior-preserving — `InheritLock()` is exactly what the default produced.

**Requirements:** R5 (green at every commit), and prerequisite for R1.

**Dependencies:** None. (Valid while the default still exists; lands and stays green on its own.)

**Files:**
- Modify: `tests/test_recorder.py` (17 call sites: lines ~386, 405, 420, 436, 463, 482, 503, 517, 549, 575, 602, 649, 683, 716, 819, 895, 939 — all omit `_lock_policy`; note `:716` sits inside a `pytest.raises(SystemExit)` block, so it must receive the policy or U2's bind-time `TypeError` would preempt the `SystemExit(1)` it asserts)
- Modify: `tests/test_recording_integration.py` (6 call sites: ~157, 681, 781, 885, 970, 1101)
- Modify: `tests/engine/test_screen_recorder_parity.py` (2 sites: ~108, 212 — already pass `_menubar_policy`, `_signal_policy`)
- Modify: `tests/engine/test_sigterm_startup_elapsed.py` (2 sites: ~84, 116 — already pass `_menubar_policy`, `_signal_policy`)
- Modify: `tests/engine/test_worker_policy_injection.py` (2 sites: ~48, 84 — already pass other `_*_policy`; adding `_lock_policy` is *more* correct for a worker-policy-injection test)
- Modify: `tests/_signal_during_setup_driver.py` (1 site: ~104 — **standalone subprocess driver, not a pytest test**; a "run pytest and fix failures" pass will not surface it)

**Approach:**
- Add `from screencap.engine.lock_policy import InheritLock` to each touched file (top-level test imports are fine — these are test modules, not the `--help`-fast CLI path).
- Add `_lock_policy=InheritLock()` to each call. For sites already passing other `_*_policy` kwargs, slot it alongside.
- No assertions or behaviors change — this is a pure, behavior-preserving migration.

**Patterns to follow:**
- `src/screencap/session.py:236` and `tests/engine/test_worker_policy_injection.py:48` — existing call sites that pass explicit `_*_policy` kwargs, including the import form.

**Test scenarios:**
- Test expectation: none new — behavior-preserving migration. Verification is the *existing* suite staying green, with each migrated test asserting the same outcomes as before.

**Verification:**
- Full `start_recording`-touching suite green with the default still present in `recorder.py`.
- `grep -rn "start_recording(" tests/ src/` shows no direct call site omitting `_lock_policy` except `session.py` (which already passes it) and non-call references (comments, monkeypatch replacements in `tests/test_session_daemon_permission_preflight.py`, method names).

---

### U2. Make `_lock_policy` required and pin the invariant

**Goal:** Drop the `InheritLock()` default so `_lock_policy` is a required keyword-only argument; update the NOTE comment to describe the enforced contract; add a test that pins the invariant directly.

**Requirements:** R1, R2, R3, R4.

**Dependencies:** U1 (every caller must already pass the policy, or U2 turns the suite red).

**Files:**
- Modify: `src/screencap/recorder.py` — signature line `:590` (`_lock_policy: "LockPolicy | None" = None` → `_lock_policy: "LockPolicy"`, required, already keyword-only after the `*`); resolution line `:643` (`lock = _lock_policy if _lock_policy is not None else InheritLock()` → `lock = _lock_policy`); the NOTE at `:636-642`; remove the now-unused `InheritLock` import at `:608` (after confirming no other reference).
- Test: `tests/test_recorder.py` — new invariant test (e.g. a `TestLockPolicyContract` class, alongside the existing `TestForceExitContracts`).

**Approach:**
- A required keyword-only parameter sitting among defaulted keyword-only parameters is valid Python (`def f(*, a=1, b): ...` makes `b` required); no reordering needed beyond keeping `_lock_policy` after the `*`.
- Rewrite the NOTE comment: state that `_lock_policy` is required precisely because `InheritLock` is a no-op that is only correct inside a daemon-spawned worker (where the supervisor owns the pidfile), and that direct callers must consciously choose a policy. Keep the pointer to `daemon/supervisor.py` as the real exclusion owner.
- Do **not** wrap the failure in `SystemExit`/`console.print`; the native `TypeError` at call binding is the intended contract.

**Execution note:** Land U1 + U2 in the same PR; U2's required-arg change depends on U1's migration to keep CI green.

**Patterns to follow:**
- `src/screencap/engine/screen_recorder.py:250-264` — `RecordingPolicies`' no-defaults docstring and stance (the precedent this change re-aligns `start_recording` with).

**Test scenarios:**
- Error path (the invariant pin): `start_recording("inv", output_dir=tmp_path / "rec")` with `_lock_policy` omitted raises `TypeError`; assert `"_lock_policy"` appears in `str(exc)`. No mocks needed — the `TypeError` is raised at call binding, before the body executes, so there are no side effects to stub.
- Happy path (regression): `start_recording("ok", output_dir=tmp_path / "rec", _lock_policy=InheritLock())` under the standard mock stack returns the 4-tuple and writes `.recording_id` / `.recording_intent` identically to pre-change. (Already exercised by the U1-migrated tests; no duplication required — note it in verification rather than re-adding.)
- Integration (production path unaffected): the daemon worker path via `session.run_recording_worker` still passes `_lock_policy=InheritLock()`; `tests/engine/test_worker_policy_injection.py` stays green. (Covered by existing suite post-U1.)

**Verification:**
- Full suite green.
- A direct `start_recording(...)` without `_lock_policy` raises `TypeError` naming `_lock_policy`.
- `recorder.py` has no leftover unused `InheritLock` import; the NOTE comment reflects enforcement.
- The daemon production path (`session.py:236`) is unchanged and still records normally.

---

## System-Wide Impact

- **Interaction graph:** `start_recording`'s only production caller is `session.run_recording_worker` (passes the policy ✓). The public CLI (`screencap start`) reaches recording via daemon → `supervisor.spawn` → `_engine-worker` → `session.run_recording_worker`, never through the bare default. CLI/MCP/SwiftUI surfaces are unaffected.
- **API surface parity:** `start_recording` is the single entry; `ScreenRecorder` / `RecordingPolicies` one layer down already require an explicit lock. No parallel entry point needs the same change.
- **External callers (verified):** `start_recording` is **not** re-exported from `src/screencap/__init__.py` and is not a documented public entry point; a grep of `src/` confirms the only importer outside `recorder.py` itself is `src/screencap/session.py`. The "zero production impact" claim rests on this — there is no out-of-tree caller to break, so the hard `TypeError` needs no deprecation window. If a future change re-exports `start_recording` as a public API, revisit whether a `DeprecationWarning`-on-default release should precede the hard requirement.
- **Error propagation:** the new failure is a `TypeError` at call binding (developer-facing, fail-fast, no partial side effects), distinct from the user-facing `SystemExit` + `console.print` path used for runtime errors.
- **State lifecycle risks:** none — behavior-preserving for every real caller; identity-file and `recording.db` lifecycles are untouched.
- **Unchanged invariants:** real recording behavior, `InheritLock` semantics, and the daemon supervisor's pidfile-based process-exclusion are all unchanged. The `capture_dir.exists()` race at `screen_recorder.py:412-416` is **not** closed by this change and remains as-is (explicitly out of scope).

---

## Risks & Dependencies

| Risk | Mitigation |
|------|------------|
| A direct call site is missed — especially `tests/_signal_during_setup_driver.py` (a standalone driver pytest will not flag) — leaving a latent `TypeError` for a non-test run. | U1 enumerates all 30 sites explicitly (research-confirmed); verification greps for any remaining omitting call site, not just "pytest passes." |
| A future contributor cargo-cults `_lock_policy=InheritLock()` for a genuine non-daemon caller and re-introduces the silent no-op. | The required arg forces a conscious choice at every call; the rewritten NOTE states `InheritLock` is daemon-only and that real exclusion is the supervisor's pidfile. Real direct-caller exclusion is deferred to the engine-topology spike. |
| Engine-topology spike later reshapes the `LockPolicy` seam, making this work churn. | Option A is forward-compatible: if the seam collapses, the param is simply deleted (required-vs-default is irrelevant to removal); if a real lock variant returns, requiring an explicit policy is *more* correct. No coupling introduced. |
| Leftover unused `InheritLock` import in `recorder.py` (not in the `engine/` ruff lane). | U2 removes it and verifies by inspection (noted in Deferred-to-Implementation). |

---

## Sources & References

- Linear: SCR-66 — Enforce `_lock_policy` intent at `start_recording` API boundary (post-SCR-53)
- Related: SCR-53 / PR #183 (`refactor(engine): remove ClaimLock`) — introduced the `InheritLock` default this plan tightens
- Engine-topology spike (open, not a blocker): `docs/tickets/2026-05-08-engine-topology-spike.md`
- Key code: `src/screencap/recorder.py:562-680`, `src/screencap/engine/lock_policy.py`, `src/screencap/engine/screen_recorder.py:250-264` & `:410-416`, `src/screencap/session.py:206-238`, `src/screencap/daemon/supervisor.py`, `src/screencap/cli/_autospawn.py`
- Learnings: `docs/solutions/integration-issues/daemon-start-failure-typed-error-before-spawn-2026-06-08.md`, `docs/solutions/runtime-errors/capture-health-nonscreen-attribution-terminal-stop-2026-06-01.md`, `docs/solutions/workflow-issues/privacy-guards-deselected-on-ci-silently-rot-2026-06-10.md`
