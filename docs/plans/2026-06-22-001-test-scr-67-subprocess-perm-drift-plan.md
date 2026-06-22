---
title: "test: Subprocess-level perm-drift integration test for daemon socket bind (SCR-67)"
type: test
status: active
date: 2026-06-22
linear: https://linear.app/zk-email/issue/SCR-67/add-subprocess-level-perm-drift-integration-test-for-daemon-socket
---

# test: Subprocess-level perm-drift integration test for daemon socket bind (SCR-67)

## Summary

Add the subprocess-level integration test deferred from the SCR-64 plan (U2 §Test scenarios scenario 5): spawn a real `screencap serve` process, induce bind-time parent-directory permission drift, and assert the OS-level exit code (`EX_TEMPFAIL == 75`) plus a stderr line naming the offender. To drive drift across the process boundary deterministically — which the `ce-code-review` fixer's F11 attempt could not, because `_ensure_socket_directory` re-`chmod`s the parent back to `0o700` — add a small, **fail-closed**, test-only env-var hook (`SCREENCAP_DAEMON_TEST_DRIFT_PARENT_MODE`) read inside `bind_unix_socket` right after the directory is ensured. The hook reproduces the exact ensure→drift→verify race the real `_verify_socket_perms` defends against, exercises real filesystem state through the real verifier, and lives in the `SCREENCAP_DAEMON_` namespace so the existing `_autospawn.py` prefix strip can never carry it into a production daemon.

---

## Problem Frame

PR #186 (SCR-64) added bind-time permission verification (`_verify_socket_perms` → `SocketPermsDrift` → `EX_TEMPFAIL`) and verified the contract two ways: 4 monkeypatch unit tests in `tests/daemon/test_socket.py`, and an in-process `serve()` test (`test_serve_against_perm_drifted_parent_dir_exits_75_with_drift_message` in `tests/test_serve_command.py`) that drives the full `SocketPermsDrift → EX_TEMPFAIL` path. What none of them prove is the **real launch path under drift**: that a genuinely spawned `screencap serve` process exits `75` at the OS level and prints the offender to stderr.

**Honest marginal value (the build-vs-skip call the ticket invited).** The generic OS-level wiring — `argv` parse, `serve()`'s `except → _print_stderr → stderr`, `EX_TEMPFAIL` exit — is *already* proven end-to-end by the sibling `test_serve_against_pre_bound_socket_exits_75_and_logs_pid`, which spawns a real daemon and asserts `returncode == 75` + a stderr match through the **same** exception dispatcher (`SocketPermsDrift` and `DaemonAlreadyRunning` both flow through `server.py:182-193`). So this test's true delta is narrower than "argv/env/signal wiring" broadly: it proves (a) the `SocketPermsDrift` branch specifically reaches the OS-level contract through a real process (a different branch than the already-tested `DaemonAlreadyRunning`), and (b) the **env-var hook itself** works across a real process boundary — env propagation plus the hook's bind-path placement behave in the real launch environment, not just under an in-process monkeypatch. That delta is real but modest; it is the basis for declining the ticket's skip option, stated plainly rather than overclaimed.

The obstacle (and why the ticket asked for design, not a mechanical add): a pre-spawn `chmod 0o755` on the parent dir is healed away by `_ensure_socket_directory`'s `chmod(parent, 0o700)` before the verify runs, so drift never triggers. Crossing the process boundary needs a hook the spawned daemon itself honors *between* ensure and verify.

---

## Requirements

- R1. A test in `tests/` spawns `screencap serve` as a real subprocess, induces bind-time perm drift, and asserts non-zero exit — specifically `EX_TEMPFAIL == 75` — plus a stderr line naming the offending path/mode.
- R2. Drift is induced through a test-only env-var hook (no new user-facing CLI surface). The hook is a no-op when unset and **fail-closed** when set: because it drifts perms *before* `_verify_socket_perms`, any non-`0o700` value aborts startup — it can never produce a *running* daemon serving over a leaky socket.
- R3. The hook's env var cannot reach a daemon on any production launch path. Across all three: **auto-spawn** — `_autospawn.py`'s existing `SCREENCAP_DAEMON_` prefix strip (`src/screencap/cli/_autospawn.py:222-223`) drops it before `posix_spawn`; **LaunchAgent** — launchd does not inherit the operator's shell environment and `launchagent.py` sets `EnvironmentVariables` to a fixed `{"PATH": ...}` allowlist, so no `SCREENCAP_DAEMON_*` var propagates; **manual `screencap serve`** — only a developer who has explicitly exported the var reaches a real daemon, and even then the worst case is fail-closed (startup refused) plus a transient run-dir mode drift that the next start self-heals (see System-Wide Impact).
- R4. No regression: the in-process `serve()` drift test, the 4 `_verify_socket_perms` unit tests, and normal `serve` happy-path startup (`serve_process` fixture tests) all still pass; the hook causes zero behavior change when its env var is unset.

**Origin acceptance examples (from SCR-67):**
- AE1 (covers R1): a `tests/daemon/` or `tests/` test spawns `screencap serve` and asserts non-zero exit (`EX_TEMPFAIL == 75`) + stderr naming the offender, in the presence of bind-time perm drift.
- AE2 (covers R2): no new public CLI surface — env var or fixture is fine; a user-facing flag is not.
- AE3 (covers R4): does not regress F1's in-process `serve()` test or the existing 4 unit tests in `test_socket.py`.

---

## Scope Boundaries

- No user-facing CLI flag for drift injection — env var only (per ticket acceptance).
- Not refactoring the existing in-process `test_serve_against_perm_drifted_parent_dir_exits_75_with_drift_message` to use the new env hook — it stays as-is (it is part of R4's no-regression set).
- Not adding subprocess-level coverage for the **socket-file** drift case (`0o644`) — one drift case (parent dir) proves the end-to-end `EX_TEMPFAIL` + stderr contract; socket-file drift stays covered by the existing in-process unit test (`test_socket_file_perm_drift_raises_and_cleans_up`).
- No changes to `SECURITY.md`, the audit log, the EUID gate, or the error envelope — that work shipped under SCR-64.
- Not changing `_autospawn.py`'s strip filter — R3 *relies* on it unchanged.

---

## Context & Research

### Relevant Code and Patterns

- `src/screencap/daemon/socket.py` — `bind_unix_socket` (lines 218-247) is the insertion point. Sequence: `_ensure_socket_directory` (mkdir + `chmod(parent, 0o700)`, lines 78-80) → `_probe_existing_socket` → `bind` (under `umask 0o077`) → `os.chmod(socket, 0o600)` → `_verify_socket_perms` (lines 87-118, raises `SocketPermsDrift` on parent != `0o700` or socket != `0o600`) → `listen`. The drift hook goes immediately after `_ensure_socket_directory`.
- `src/screencap/daemon/server.py` — `serve()` catches `SocketPermsDrift` and returns `EX_TEMPFAIL` (= 75, line 20), printing `str(exc)` to stderr via `_print_stderr` → `Console(stderr=True).print(...)` (lines 191-193, 199-202). This is the contract the subprocess test asserts.
- `src/screencap/daemon/server.py:55` — `SCREENCAP_DAEMON_SIGNAL_READY_MARKER`: an existing **test-only env hook read in the daemon startup path**. Direct precedent for adding a narrow test hook in production daemon code.
- `src/screencap/daemon/supervisor.py:173` — `SCREENCAP_DAEMON_ENGINE_COMMAND`: an existing `SCREENCAP_DAEMON_`-namespace override hook (the one `_autospawn.py` strip explicitly defends against). Confirms both the namespace convention and the strip's purpose.
- `src/screencap/cli/_autospawn.py:214-224` — strips every `SCREENCAP_DAEMON_*` env var (prefix match) before `posix_spawn`. R3 inherits this for free.
- `tests/test_serve_command.py:93` — `test_serve_against_pre_bound_socket_exits_75_and_logs_pid`: the sibling subprocess test that asserts exit-75 + a stderr `re.search`. The new test mirrors its shape.
- `tests/test_serve_command.py:129-161` — `test_serve_against_perm_drifted_parent_dir_exits_75_with_drift_message`: the in-process analogue; its `ensure_then_drift` wrapper (line 150) is exactly what the env hook makes subprocess-crossable.
- `tests/daemon/conftest.py` — `short_socket_path` (line 97, `/tmp/sc-<hex>` symlink to dodge the 103-byte UDS limit), `wait_for_socket` (line 118), `daemon_env`/`cli_env` (the spawn env with `PYTHONPATH=src`). These are the harness.
- `tests/daemon/test_socket.py:135-182` — the existing parent-dir and socket-file drift unit tests (monkeypatch-based); the new in-process unit test for the hook sits beside them.
- `tests/cli/test_autospawn.py` — `test_no_launchagent_spawns_and_polls_ready` (line 47) monkeypatches `posix_spawn`; the candidate seam for an explicit strip assertion (R3).

### Institutional Learnings

- `docs/solutions/design-patterns/fallback-path-recovery-and-exit-code-signaling-2026-06-15.md` — the project's exit-code-signaling convention (`EX_TEMPFAIL` vs `1`) that this test pins at the OS level.
- `docs/solutions/integration-issues/daemon-start-failure-typed-error-before-spawn-2026-06-08.md` — typed daemon-start failures surface as exit codes + stderr before the server loop; reinforces asserting on the exit code + stderr pair.
- Testing-in-a-worktree note (project memory): run with `PYTHONPATH=src` because the editable install may point at whichever worktree last ran `pip install -e`. `daemon_env`/`cli_env` already set `PYTHONPATH=src` for the spawned subprocess; ensure the in-process U1 test run uses the same override.

---

## Key Technical Decisions

- **Env-var drift hook in the bind path (chosen; user-confirmed).** A test-only `SCREENCAP_DAEMON_TEST_DRIFT_PARENT_MODE`, read by a small helper called right after `_ensure_socket_directory`, `chmod`s the parent to the given (octal) mode. Deterministic, exercises real filesystem state through the real `_verify_socket_perms`, and is the subprocess-crossable analogue of the in-process test's `ensure_then_drift`. Chosen over the ticket's other options (see Alternatives).
- **Placement: after `_ensure_socket_directory`, before `_probe_existing_socket`.** This reproduces the exact ensure→drift→verify race the verifier exists to catch. Probe/bind/`chmod(socket)` touch the socket file and the parent only at `0o600`/no-op respectively, so the parent drift persists untouched to the verify step.
- **Fail-closed by construction.** Because the drift lands *before* the verify, any non-`0o700` value makes `_verify_socket_perms` raise and `serve()` exit `75` — the hook can only ever *refuse* startup, never serve over a drifted socket. (A value of `0o700` is indistinguishable from no-op.) This is what makes a test hook in production bind code **harmless**, not what makes it valuable: the hook adds zero production defensive value (no detection, no hardening) — its only merit is deterministic, process-crossable test reachability. It is inert in production on all three launch paths (auto-spawn strip; launchd's non-inheriting fixed env allowlist; manual invocation requires an explicit export and is still fail-closed). The ticket's stated "con" (another `SCREENCAP_DAEMON_*` var) is thus neutralized rather than turned into a benefit.
- **Parent-dir drift only at the subprocess level.** One drift case proves the OS-level `EX_TEMPFAIL` + stderr contract end to end; socket-file drift remains covered in-process. Avoids a near-duplicate subprocess test.
- **Assert on wrap-safe stderr tokens, not the full path.** `_print_stderr` uses `rich.Console(stderr=True)`, which wraps at ~80 cols when stderr is a pipe and can split a long `/private/var/folders/...` path mid-line. Whitespace-delimited tokens (`drifted`, `0o755`) stay intact across wrapping.

---

## Open Questions

### Resolved During Planning

- **Which drift mechanism?** Env-var hook that drifts the parent dir after `_ensure_socket_directory` (user-confirmed). See Alternatives for the rejected options.
- **Where does the hook live?** `bind_unix_socket` in `src/screencap/daemon/socket.py`, between ensure and probe — the only place that can inject between the two steps the verifier brackets.
- **One drift case or several at the subprocess level?** One (parent dir). Socket-file drift stays in-process.
- **Run-dir mode residue after abort?** Accepted in v1, not restored. `cleanup_socket` unlinks only the socket file, so the parent is left at the drifted mode until the next start's `_ensure_socket_directory` self-heals it. The window is bounded, tmp-only in tests, and only reachable when the var is explicitly set (never on a real production launch path — see R3). Restore-on-abort is recorded as a deferred alternative below rather than added to production cleanup for a test-only hook.

### Deferred to Implementation

- Exact stderr assertion tokens — confirm against rich's real piped output (default width; possible style stripping). Default to membership checks for `drifted` and `0o755`; widen via `COLUMNS` only if a token proves to wrap.
- R3 enforcement form — prefer extending `tests/cli/test_autospawn.py`'s `posix_spawn`-monkeypatch test to assert a `SCREENCAP_DAEMON_TEST_*` var is absent from the spawned env. Note: the existing `fake_spawn` stub there records `path`/`argv`/`setsid` but **discards `env`** — add `env` capture to the stub before this assertion is possible. Fall back to a `startswith("SCREENCAP_DAEMON_")` assertion in `tests/daemon/test_socket.py` if the env-capture extension is undesired. Pick at implementation time.
- Restore-on-abort (deferred alternative to the accepted v1 residue): have the hook record the parent's pre-drift mode and a `try/finally` (or the `except SocketPermsDrift` block in `bind_unix_socket`) re-`chmod` it back before re-raising. Only worth adding if the bounded residue is ever judged unacceptable; the verify must still observe the drift first.
- Whether to import `EX_TEMPFAIL` from `screencap.daemon.server` for the assertion (self-documenting) or hardcode `75` (matches the sibling `test_serve_against_pre_bound_socket_exits_75_and_logs_pid`). Either is acceptable.

---

## High-Level Technical Design

> *This illustrates the intended approach and is directional guidance for review, not implementation specification. The implementing agent should treat it as context, not code to reproduce.*

```
bind_unix_socket(socket_path):
    _ensure_socket_directory(socket_path)        # mkdir + chmod(parent, 0o700)
    _maybe_inject_test_parent_drift(socket_path) # NEW: no-op unless
                                                 #   SCREENCAP_DAEMON_TEST_DRIFT_PARENT_MODE set
                                                 #   → chmod(parent, <octal mode>)
    _probe_existing_socket(socket_path)          # unchanged
    listener.bind(...) under umask 0o077         # unchanged (touches socket, not parent)
    os.chmod(socket_path, 0o600)                 # unchanged
    _verify_socket_perms(socket_path)            # parent != 0o700  →  SocketPermsDrift
        └── on raise: listener.close(); cleanup_socket(); re-raise
    listener.listen(...)

serve(socket_path):                              # unchanged
    except SocketPermsDrift as exc:
        _print_stderr(str(exc))                  # "socket parent dir perms drifted: ... 0o755 ..."
        return EX_TEMPFAIL  # 75

Test path (subprocess):
    subprocess.run(["...serve", "--socket", S],
                   env={..., "SCREENCAP_DAEMON_TEST_DRIFT_PARENT_MODE": "0755"})
        → exit 75, stderr contains "drifted" + "0o755", socket file cleaned up

Production safety:
    _autospawn.py strips SCREENCAP_DAEMON_*  →  hook absent from real daemon
    + fail-closed: any drift value only ABORTS startup, never serves leaky
```

---

## Implementation Units

### U1. Test-only parent-dir drift hook in the bind path

**Goal:** Provide a deterministic, fail-closed way to induce parent-directory perm drift across a process boundary, so the real `_verify_socket_perms` runs against real filesystem state in a spawned daemon.

**Requirements:** R2, R3, R4

**Dependencies:** None

**Files:**
- Modify: `src/screencap/daemon/socket.py`
- Test: `tests/daemon/test_socket.py`
- Test: `tests/cli/test_autospawn.py` *(only if extending the strip test is the chosen R3 form)*

**Approach:**
- Add a module constant `_TEST_DRIFT_PARENT_MODE_ENV = "SCREENCAP_DAEMON_TEST_DRIFT_PARENT_MODE"` and a small helper `_maybe_inject_test_parent_drift(socket_path)` that returns immediately unless the env var is set, otherwise `os.chmod(socket_path.parent, int(raw, 8))` (octal; accepts both `"0755"` and `"755"`).
- Call the helper in `bind_unix_socket` immediately after `_ensure_socket_directory(socket_path)` and before `_probe_existing_socket`.
- Give the helper a docstring stating: test-only; reproduces the ensure→drift→verify race; fail-closed (drift lands before verify, so any non-`0o700` value aborts startup and can never serve over a leaky socket); namespaced under `SCREENCAP_DAEMON_` so `_autospawn.py` strips it from production spawns. Reference SCR-67.

**Execution note:** Mechanical and well-bounded — add the in-process drift-via-env unit test (red), then the helper (green).

**Patterns to follow:**
- `src/screencap/daemon/server.py:55` (`SCREENCAP_DAEMON_SIGNAL_READY_MARKER`) — existing test-only env hook in daemon startup.
- `src/screencap/daemon/supervisor.py:173` (`SCREENCAP_DAEMON_ENGINE_COMMAND`) — namespace + strip rationale.
- `tests/daemon/test_socket.py:135-159` (`test_parent_dir_perm_drift_raises_and_cleans_up`) — the assertion shape for the new in-process test (raises `SocketPermsDrift` matching `0o755`, socket file cleaned up).

**Test scenarios:**
- Happy path / default-off: env var unset → `bind_unix_socket(daemon_socket_path)` succeeds, parent stays `0o700`, socket `0o600` (hook is a no-op; pins R4 — no behavior change when unset).
- Drift via env: `monkeypatch.setenv("SCREENCAP_DAEMON_TEST_DRIFT_PARENT_MODE", "0755")` → `bind_unix_socket` raises `SocketPermsDrift` matching `r"0o755"`; the half-bound socket file does not survive (`not daemon_socket_path.exists()`). Proves the hook reaches the real `_verify_socket_perms` (R2).
- Fail-closed equivalence: env var = `"0700"` → bind succeeds and parent is `0o700` — confirms the hook cannot yield a *running* daemon with drifted perms (setting the expected mode == no-op).
- Edge case — malformed value: env var = `"notoctal"` → `int(raw, 8)` raises `ValueError` during bind (acceptable: test-only misuse, surfaces loudly rather than silently). Assert it does not silently succeed/serve. *(Include only if cheap; the value is always test-controlled.)*
- Autospawn-strip guard (R3): assert a `SCREENCAP_DAEMON_TEST_*` var present in `os.environ` is absent from the env `_autospawn.py` builds for `posix_spawn` (extend `tests/cli/test_autospawn.py`'s monkeypatched-`posix_spawn` test — its `fake_spawn` stub currently records `path`/`argv`/`setsid` but discards `env`, so add `env` capture first); or, as fallback, assert `_TEST_DRIFT_PARENT_MODE_ENV.startswith("SCREENCAP_DAEMON_")` in `tests/daemon/test_socket.py`.

**Verification:**
- `pytest tests/daemon/test_socket.py` green; the 4 pre-existing `_verify_socket_perms` tests unchanged and passing.
- With the env var unset, no observable change to `bind_unix_socket` behavior.

---

### U2. Subprocess-level perm-drift integration test (SCR-67 deliverable)

**Goal:** Spawn a real `screencap serve` process, induce bind-time parent-dir drift via the U1 hook, and assert the OS-level exit code (`EX_TEMPFAIL == 75`) and a stderr line naming the offender.

**Requirements:** R1, R4

**Dependencies:** U1

**Files:**
- Test: `tests/test_serve_command.py`

**Approach:**
- Add `test_serve_subprocess_perm_drift_exits_75_with_named_offender(cli_env, tmp_path)` adjacent to the in-process sibling (`test_serve_against_perm_drifted_parent_dir_exits_75_with_drift_message`) and the rogue/pre-bound subprocess tests.
- `socket_path = short_socket_path(tmp_path)` — no pre-creation needed; `_ensure_socket_directory` mkdirs and the hook then drifts.
- `subprocess.run(_cli_command("serve", "--socket", str(socket_path)), env={**cli_env, "SCREENCAP_DAEMON_TEST_DRIFT_PARENT_MODE": "0755"}, stdout=PIPE, stderr=PIPE, text=True, timeout=10)`.
- Assert `result.returncode == EX_TEMPFAIL` (import from `screencap.daemon.server`, or hardcode `75` matching the sibling).
- Assert wrap-safe tokens in stderr: `"drifted" in result.stderr` and `"0o755" in result.stderr` (not the full path — see Key Technical Decisions).
- Assert `not socket_path.exists()` (the drift path runs `cleanup_socket`).
- Clean up `socket_path` in a `finally` (mirror the siblings).

**Execution note:** This is the acceptance test. Written before U1's hook exists it goes red (drift never triggers, daemon binds normally); it turns green once U1 lands. Author U1 → U2, or U2-red → U1; the dependency is strict either way.

**Patterns to follow:**
- `tests/test_serve_command.py:93-126` (`test_serve_against_pre_bound_socket_exits_75_and_logs_pid`) — subprocess exit-75 + stderr-assertion shape, `_cli_command`, `cli_env`, `timeout`.
- `tests/daemon/conftest.py` — `short_socket_path`, `cli_env`.

**Test scenarios:**
- Covers AE1 / Happy path (the deliverable): drift env set → real `screencap serve` subprocess exits `75`; stderr contains `drifted` and `0o755`; the half-bound socket file is cleaned up.
- Default-off at subprocess level (R4): covered by the existing `serve_process` fixture tests (`test_serve_accepts_same_euid_and_returns_default_404`, `test_sigterm_during_accept_loop_unlinks_socket`) — without the env var, a real `serve` binds and runs normally. Do **not** add a near-duplicate; note the coverage instead.

**Verification:**
- `PYTHONPATH=src pytest tests/test_serve_command.py` green on macOS.
- F1's in-process drift test and the rogue/pre-bound subprocess tests are unaffected (run the whole file).

---

## System-Wide Impact

- **Interaction graph:** `bind_unix_socket` gains one conditional helper call between `_ensure_socket_directory` and `_probe_existing_socket`. No effect on the accept loop, the EUID gate, the audit log, the idle-shutdown watchdog, or any `/v0/*` verb.
- **Error propagation:** Hook-induced drift propagates exactly like real drift — `SocketPermsDrift` → `serve()` returns `EX_TEMPFAIL` (75) → process exit. No new error type, code, or path.
- **State lifecycle:** On the drift path `bind_unix_socket` already `listener.close()` + `cleanup_socket()`; the hook adds no new cleanup obligation. The half-bound socket is unlinked; the tmp parent dir is removed by the fixture. **Residue note:** `cleanup_socket` unlinks only the socket *file* — it does not restore the parent dir's mode, so after an abort the parent is left at the drifted mode on disk until the next `_ensure_socket_directory` re-`chmod`s it to `0o700`. In tests the parent is `tmp_path` (removed by the fixture); on a real path the next `screencap serve` self-heals it. The window is therefore bounded and only reachable when the var is explicitly set — accepted in v1 rather than adding restore-on-abort logic to production cleanup (see Open Questions for the deferred restore-on-abort alternative).
- **API surface parity:** No client-side or wire change. `--socket` is already a hidden test-only flag; no new flag. Swift `DaemonClient`, CLI `_daemon_client.py`, and future MCP are untouched. Schema versions unchanged.
- **Unchanged invariants:** `_verify_socket_perms`, the EUID accept-time gate, the error envelope (`errors.py`), schema versions, and the `_autospawn.py` `SCREENCAP_DAEMON_` strip filter (R3 *depends on it* unchanged). The hook is fail-closed and inert on **every** production launch path — auto-spawn (stripped before `posix_spawn`), LaunchAgent (launchd does not inherit the operator shell env and `launchagent.py` pins `EnvironmentVariables` to a fixed `{"PATH": ...}` allowlist), and manual `screencap serve` (reachable only via an explicit export, and still fail-closed). A production daemon's behavior is therefore provably identical with or without the change on all three paths.

---

## Risks & Dependencies

| Risk | Likelihood | Impact | Mitigation |
|------|-----------|--------|------------|
| `rich` stderr wrapping splits an asserted substring | Medium | Low | Match wrap-safe whitespace-delimited tokens (`drifted`, `0o755`), never the full path; widen via `COLUMNS` only if a token wraps. |
| Reviewer objects to a test-only env hook in production bind code | Low | Low | Cite the two existing `SCREENCAP_DAEMON_*` test/override hooks (`server.py:55`, `supervisor.py:173`), the fail-closed property (drift before verify → can only abort), and the complete three-path containment (auto-spawn strip + launchd non-inheriting fixed-env allowlist + manual-only-via-explicit-export). Keep the hook a single named helper with a clear docstring; strictly less dangerous than the existing `SCREENCAP_DAEMON_ENGINE_COMMAND` hook, which can execute arbitrary code. |
| Subprocess test flakes under CI load (spawn/teardown) | Low | Low | No `wait_for_socket` race in the drift path — the daemon exits fast; `subprocess.run(timeout=10)` bounds it. Mirrors the already-stable `test_serve_against_pre_bound_socket` shape. |
| Non-macOS CI portability | Low | Low | The drift path is pure POSIX (`chmod`/`stat`); no `lsof` dependency (unlike the rogue-PID test). macOS remains primary. |

---

## Alternative Approaches Considered

The SCR-67 ticket enumerated three designs and an explicit skip option; the chosen approach is a refinement of its option 1 (drift *after* ensure rather than disabling ensure).

- **Disable `_ensure_socket_directory` via env var (ticket option 1):** test pre-`chmod`s the parent to `0o755` and the daemon skips the self-heal so it survives to verify. Viable, but less faithful — it removes the `0o700` self-heal entirely and forces the test to pre-create the dir. The chosen post-ensure-drift hook reproduces the real ensure→drift→verify race more precisely with the same surface.
- **Inject the mode `_verify_socket_perms` sees (ticket option 2):** an env var forces the mode the verifier reads regardless of fs state. Rejected — it stubs the very `stat`/compare under test, the lowest-fidelity option.
- **Race fixture, no production hook (ticket option 3):** drive the `chmod` from the test via a fifo/sentinel the daemon waits on between ensure and verify. Rejected — timing-sensitive and historically flaky in CI; its only upside (no production code change) is outweighed by nondeterminism, and the chosen hook is fail-closed + stripped, so the production-code cost is negligible.
- **Skip entirely (ticket scope note):** conclude the in-process `serve()` test + 4 unit tests suffice. Considered and declined, but on a narrower basis than "argv/env/signal wiring": the sibling `test_serve_against_pre_bound_socket` already proves the generic OS-level dispatch, so the real delta is the `SocketPermsDrift`-specific branch through a real process plus end-to-end exercise of the env-hook across the boundary (see Problem Frame → *Honest marginal value*). That delta is modest; the cost is one tiny fail-closed hook plus one test, which tips the call toward building it. A reasonable reviewer could still prefer skip — if so, that is a one-line decision, not a re-plan.

---

## Sources & References

- **Linear ticket:** [SCR-67](https://linear.app/zk-email/issue/SCR-67/add-subprocess-level-perm-drift-integration-test-for-daemon-socket)
- **Origin plan (U2 §Test scenarios scenario 5):** `docs/plans/2026-05-25-001-fix-daemon-socket-per-caller-auth-plan.md`
- **Related ticket:** [SCR-64](https://linear.app/zk-email/issue/SCR-64/daemon-socket-has-no-per-caller-auth-pr-180-made-daemon-side-the-sole)
- **Production code:** `src/screencap/daemon/socket.py`, `src/screencap/daemon/server.py`, `src/screencap/cli/_autospawn.py`, `src/screencap/cli/__init__.py` (serve command)
- **Existing coverage:** `tests/test_serve_command.py` (in-process drift + rogue/pre-bound subprocess), `tests/daemon/test_socket.py` (4 `_verify_socket_perms` unit tests), `tests/daemon/conftest.py` (spawn harness), `tests/cli/test_autospawn.py` (strip seam)
- **Institutional learnings:** `docs/solutions/design-patterns/fallback-path-recovery-and-exit-code-signaling-2026-06-15.md`, `docs/solutions/integration-issues/daemon-start-failure-typed-error-before-spawn-2026-06-08.md`
