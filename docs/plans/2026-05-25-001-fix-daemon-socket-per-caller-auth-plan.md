---
title: "fix: Document daemon socket trust boundary (SCR-64)"
type: fix
status: completed
date: 2026-05-25
linear: https://linear.app/zk-email/issue/SCR-64/daemon-socket-has-no-per-caller-auth-pr-180-made-daemon-side-the-sole
---

# fix: Document daemon socket trust boundary (SCR-64)

## Summary

Ratify same-EUID + filesystem permissions as the daemon socket's documented trust boundary (matching `ssh-agent`, `gpg-agent`, Docker, and Apple's own non-XPC daemons), and harden surrounding hygiene: bind-time permission verification, peer-provenance audit log on privileged verbs, and a public `SECURITY.md`. The plan compares all three approaches the SCR-64 ticket surfaced (status quo, promote-provenance-to-gate, bearer-token via SMAppService) in `## Alternative Approaches Considered`, with recommendation rationale grounded in prior daemon-architecture plans and external research.

---

## Problem Frame

PR #180 (SCR-54 — macOS helper permission ownership) made the daemon the sole in-app authority for recording-permission gating. Before PR #180, the SwiftUI app pre-checked TCC state before any daemon recording attempt; after the merge, the daemon-side `PeerCheckingUnixSocket.accept()` EUID check is the only boundary between a same-UID process and `/v0/recording.start`. This was true before PR #180 too (the daemon socket has always been same-UID trust), but PR #180 concentrated responsibility there, which made the absence of any explicit threat-model statement load-bearing.

SCR-64's actual ask is a **decision** the codebase has not formally taken: is same-UID-trust acceptable for ScreenCap, or do we want one of two alternative auth surfaces (audit-token-style peer check, bearer token co-installed via SMAppService)? Both Phase 1 and Phase 2 daemon plans named these as deferred — Phase 1's `## Scope Boundaries` → `Deferred to Follow-Up Work` flagged future MCP introducing a second caller; Phase 2's `## Scope Boundaries` named "server-validated peer identity as auth gate" as a future iteration. This plan resolves the deferred question, documents it, and ships the defense-in-depth hygiene that comes with treating same-UID as the explicit boundary instead of an unstated default.

---

## Requirements

- R1. The plan must compare all three approaches surfaced in SCR-64 (accept same-UID + document; promote provenance to a gate; bearer token via SMAppService) with explicit rejection rationale for the two non-chosen options, anchored in prior daemon-architecture plans and external prior art.
- R2. The plan must produce a public, discoverable threat-model artifact at `SECURITY.md` that names the trust boundary (EUID + filesystem perms), enumerates in-scope and out-of-scope threats, and references the alternatives considered.
- R3. The daemon must verify socket and parent-directory permissions at bind time and fail loudly on drift (defense-in-depth against umask races and tampered installs). No behavior change on the happy path.
- R4. The daemon must persist a per-call audit record (timestamp, verb, peer PID, peer binary path, classification, outcome) for every privileged verb (`/v0/recording.start`, `/v0/recording.stop`) so operators have forensic capability without the auth surface changing.
- R5. Existing clients (Swift `DaemonClient`, CLI `_daemon_client.py`, CLI auto-spawn at `src/screencap/cli/_autospawn.py`) keep working without any header, token, or env-var change. No regression in F3 install / cron `screencap status` paths.
- R6. The `screencap serve --install` post-install verifier (`_daemon_info_responds`) and the auto-spawn smoke test must still pass — the audit/perm work must not break `/v0/daemon.info` or the LaunchAgent boot path.

---

## Scope Boundaries

- Implementing a per-call bearer token or any in-band auth header — explicitly rejected in this plan; see `## Alternative Approaches Considered`. Revisitable via a follow-up ticket if the threat model changes.
- Implementing code-signing requirement verification (`SecCodeCopyGuestWithAttributes` + Team-ID anchors) — the only mechanism that meaningfully raises the bar against a same-UID attacker, but it's blocked by ScreenCap's mixed distribution model (CLI may be unsigned/ad-hoc-signed in Homebrew/dev paths) and demands its own design pass.
- Promoting `derive_started_by_from_asgi_scope` from advisory provenance into an enforcement gate — explicitly considered and rejected (see Alternatives). Provenance stays advisory.
- TCC / permission-controller logic — orthogonal; lives under SCR-54 and the `docs/superpowers/specs/2026-05-15-scr-54-macos-permission-ownership-design.md` design.
- MCP server (`screencap mcp`) auth wiring — the binary doesn't exist yet; this plan defines the auth surface MCP will inherit on arrival but adds no MCP code.
- Audit-log rotation, retention, or shipping. The v1 audit log is append-only at `~/.screencap/run/audit.log`; rotation is deferred to a follow-up ticket if log size becomes a problem.
- Swift-side behavior change. The Swift app's `DaemonClient` and privacy pane stay untouched.

### Deferred to Follow-Up Work

- Capture this work as institutional knowledge in `docs/solutions/security/` after merge — follow-up `/ce-compound` run, separate diff.
- Per-caller code-signing verification (Option C-prime) if/when CLI distribution model formalizes around a single signed binary.
- Audit-log rotation policy (size cap, age cap, optional shipping target).

---

## Context & Research

### Relevant Code and Patterns

- `src/screencap/daemon/socket.py` — `PeerCheckingUnixSocket.accept()` (lines 157-169) is the current and only auth gate via `getpeereid()`. `bind_unix_socket` sets `0o700` on parent dir and `0o600` on the socket file. This module is where U2's bind-time re-verification lives.
- `src/screencap/daemon/provenance.py` — already extracts peer effective PID via `getsockopt(SOL_LOCAL, LOCAL_PEEREPID)`, binary path via `proc_pidpath`, and argv via `sysctl(KERN_PROCARGS2)`. Currently used only as advisory `started_by` metadata on recording-start. Module docstring (lines 14-22) explicitly disclaims auth-gating use. U3 reuses this classifier as input to audit-log records without changing its advisory disposition.
- `src/screencap/daemon/_idle_shutdown.py` — `_ActivityMiddleware(BaseHTTPMiddleware)` attached via `app.add_middleware()` (line 87) is the precedent pattern for a Starlette cross-cutting concern. The audit-logging hook can follow the same shape but does not need full middleware since it only fires on two routes.
- `src/screencap/daemon/app.py` — `recording_start` (line 219) and `recording_stop` (line 269) are the route handlers that need audit-log emission. The existing `provenance.derive_started_by_from_asgi_scope(request.scope)` call at line 243 is the integration point.
- `src/screencap/daemon/errors.py` — canonical error envelope module. Existing convention: lowercase_snake string code + typed `DaemonAPIError` subclass + free-function envelope builder (see `LOCK_CONTENDED`, `NOT_OWNED_BY_DAEMON`, etc.). No 401/403 precedent today. This plan adds no new error code (no auth rejection path is introduced).
- `tests/daemon/conftest.py` — `daemon_socket_path` (short `/tmp/sc-<hex>` symlink to dodge macOS' 103-byte UDS limit), `serve_process` (spawns `screencap serve --socket=...` subprocess), and `uds_client_factory` (`httpx.AsyncClient` with `AsyncHTTPTransport(uds=…)`) are the harness for any new socket-binding or end-to-end test.
- `tests/daemon/test_socket.py` — `test_peer_euid_check_is_invoked_and_rejects_mismatch` (lines 121-149) monkeypatches `daemon_socket.peer_matches_current_euid`. U2's permission-verification tests extend this file with the same monkeypatch pattern.
- `tests/daemon/test_recording_start_provenance.py` — existing precedent for asserting that recording-start records peer provenance. U3's audit-log assertions follow the same shape.
- `src/screencap/cli/_autospawn.py` — auto-spawn for F3 install; strips all `SCREENCAP_DAEMON_*` env vars (lines 220-224) before `posix_spawn`. R5 honors this — no new env var is introduced.
- `src/screencap/daemon/launchagent.py` — `render_plist`, `install`, `_daemon_info_responds`. R6 requires the verifier (lines 459-493) keeps working — `/v0/daemon.info` MUST remain unauthenticated.

### Institutional Learnings

- `docs/solutions/runtime-errors/eventbus-late-listener-replay-2026-05-12.md` — auth-aware paths on `/v0/*` must not interfere with cursor replay on `/v0/events`. The audit log is fire-and-forget for the privileged-verb routes only; `/v0/events` stays untouched.
- `docs/solutions/runtime-errors/launchd-plist-tilde-expansion-2026-05-09.md` — any path written into a LaunchAgent plist or read on launchd-spawn must be fully resolved (no `~` or `$HOME`). The audit-log path is opened by the daemon process itself (not via plist), but its parent dir creation must use `Path.home()` resolution at runtime to match `_AUTO_LOG_PATH` in `_autospawn.py`.
- `docs/solutions/build-errors/macos-ad-hoc-signing-tcc-rebuild-treadmill.md` — identity-by-code-signature is unstable for dev builds. Directly supports the rejection of Option C-prime (code-signing peer auth) from this plan's scope.
- `docs/solutions/integration-issues/macos-foundation-process-pipe-pitfalls.md` — spawned helpers don't inherit `.app` bundle TCC identity. Reinforces the "daemon owns capability; clients authenticate as peers" framing for `SECURITY.md`.
- Phase 1 plan (`docs/plans/2026-05-08-001-feat-daemon-architecture-phase-1-plan.md`) line 58: "Auth tokens, ACLs, capability-based permissions beyond same-user `getpeereid` origin check" is out of scope for v1. Line 73 explicitly predicts SCR-64: "Phase 2's MCP integration introduces a second daemon caller; at that point, server should validate ... rather than trusting the field." Line 133: "filesystem perms (0700 dir, 0600 socket) are the v1 authorization surface; `getpeereid` is defense-in-depth."
- Phase 2 plan (`docs/plans/2026-05-08-002-feat-daemon-architecture-phase-2-plan.md`) line 66: "Server-validated peer identity as auth gate ... is a future privacy/security iteration if multi-user contexts emerge." Line 669: "Promoting peer-PID to auth would require addressing PID reuse / TOCTOU and would block on a credential model not yet designed."

### External References

- Apple Platform Security Guide (March 2026 edition) — treats user-account / EUID as the primary data-protection boundary; layered controls (App Sandbox, TCC, Hardened Runtime, code signing) are additive when finer granularity is needed. Cited in `SECURITY.md` U1.
- Apple DevForums thread 74498 — "no inherent security difference between XPC and socket-based IPC"; daemon author owns the responsibility for code-signing checks above the EUID layer.
- TN3127 (Inside Code Signing: Requirements) — current Apple guidance on code-signing requirement strings; cited as the only mechanism that meaningfully constrains a same-UID attacker, in the Alternatives rejection of Option C-prime.
- [Scott Knight — Audit tokens explained](https://knight.sc/reverse%20engineering/2020/03/20/audit-tokens-explained.html) and [Quarkslab — Intego LPE writeup](https://blog.quarkslab.com/intego_lpe_macos_2.html) — canonical macOS PID-reuse / TOCTOU exploit references. Cited in the Option B rejection.
- Industry comparables — Docker daemon (`/var/run/docker.sock`, group + UID, no per-caller), `ssh-agent` and `gpg-agent` (per-user socket dir + 0o600, same-UID trust), 1Password CLI (Keychain via desktop helper, not file-based bearer). All ship same-UID trust as the boundary for local-socket IPC; cited in `SECURITY.md` U1 as the industry baseline.

---

## Key Technical Decisions

- **Same-EUID + filesystem permissions is the trust boundary; document it explicitly.** Matches `ssh-agent`, `gpg-agent`, Docker, and Apple's own non-XPC daemons. Phase 1 and Phase 2 plans already named this as the v1 position; this plan formalizes it.
- **Defense-in-depth via bind-time re-verification, not a new auth primitive.** Bind-time `stat()` of the socket and parent dir against expected modes catches umask drift, accidental `chmod` post-install, and a tampered run-dir. Aborts daemon startup loudly (logs + EX_TEMPFAIL exit) rather than serving over a leaky socket.
- **Audit log persists peer provenance on privileged verbs only.** Same JSON-lines shape as the existing recording events; written to `~/.screencap/run/audit.log` (mode 0o600) alongside `auto-serve.log`. Records `recording.start` and `recording.stop` outcomes. Read-only verbs (`/v0/recording.list`, `/v0/session.snapshot`, `/v0/daemon.info`, `/v0/events`) are intentionally not audited — they leak no capability and would 10x log volume.
- **No new error code, no new HTTP status.** This plan does not introduce a rejection path. Auth surface is unchanged (still EUID at accept-time). The error envelope module gets no edits.
- **Audit log is best-effort.** A failure to open or write the audit log MUST NOT fail the underlying recording verb. Log a `logger.warning` and continue. This is fire-and-forget telemetry, not a control-plane prerequisite.
- **No env var, no header, no client-side change.** R5 requires the Swift `DaemonClient`, CLI `_daemon_client.py`, and `_autospawn.py` work unchanged. The audit log is purely server-side. SECURITY.md links from `README.md` and `CLAUDE.md` so contributors find it.

---

## Open Questions

### Resolved During Planning

- **Should we promote provenance to a gate?** No. Phase 1 (line 157) and Phase 2 (line 669) plans both already considered and rejected this on PID-reuse / TOCTOU grounds, and external research confirms there is no race-free way to do PID-based gating on AF_UNIX without code-signing checks (see `## Alternative Approaches Considered` → Option B).
- **Should we add a bearer token via SMAppService?** No. A 0o600 token file in the same dir as the socket is readable by anyone who can connect to the socket, so it adds no defense against the threat that motivates it (see Option C in Alternatives). The only mechanism that meaningfully raises the bar against a same-UID attacker is code-signing requirement verification, which has its own distribution-model blockers (Option C-prime in Alternatives).
- **Does `/v0/daemon.info` need to be exempted from anything?** Not in this plan — no new gate is introduced. The post-install verifier in `launchagent.py::_daemon_info_responds` keeps working unchanged.
- **Where should `SECURITY.md` live?** Repo root, GitHub-discoverable. GitHub auto-surfaces `SECURITY.md` in the "Security" tab and on the repo policy page, which is the right discoverability for a public-facing threat-model artifact.
- **Audit log path and mode?** `~/.screencap/run/audit.log`, mode `0o600`, same parent dir as `auto-serve.log` (which already uses this mode). Parent dir already has `0o700` via `bind_unix_socket._ensure_socket_directory`.

### Deferred to Implementation

- Exact JSON-lines schema for an audit record beyond the named fields (timestamp, verb, peer_pid, peer_path, classification, outcome) — settled when writing U3 against the existing `provenance.derive_started_by_from_asgi_scope` shape.
- Whether to emit a single audit line on verb completion (post-supervisor.spawn) or a start/finish pair — single-line on completion is the default; revisit if a long-running `recording.start` makes a no-completion case operationally confusing.
- Whether bind-time perm re-verification should also validate UID/GID ownership of the socket file (vs. just mode bits) — likely yes for the parent dir, decide for the socket file at implementation time based on how `bind_unix_socket` interacts with `umask` under launchd.

---

## High-Level Technical Design

> *This illustrates the intended approach and is directional guidance for review, not implementation specification. The implementing agent should treat it as context, not code to reproduce.*

```
┌──────────────────────────────────────────────────────────────────────┐
│ Same-UID process                                                      │
│  (Swift app / CLI / future MCP / curious script)                      │
└──────────────────────────────────────┬───────────────────────────────┘
                                       │  connect() to ~/.screencap/run/api.sock
                                       │  (filesystem perms gate: 0o700 dir + 0o600 sock)
                                       ▼
┌──────────────────────────────────────────────────────────────────────┐
│ Daemon (unchanged trust boundary)                                     │
│  PeerCheckingUnixSocket.accept()                                      │
│   └── getpeereid() == EUID gate  ◄──── unchanged                      │
│                                                                       │
│  bind_unix_socket()                                                   │
│   ├── _ensure_socket_directory (0o700)  ◄──── unchanged               │
│   ├── chmod 0o600 on socket             ◄──── unchanged               │
│   └── verify_perms_after_bind  ◄──── NEW (U2): re-stat both,          │
│        abort on drift                                                  │
│                                                                       │
│  /v0/recording.start, /v0/recording.stop                              │
│   ├── existing supervisor.spawn / .stop  ◄──── unchanged              │
│   └── audit_log.record_verb(...)  ◄──── NEW (U3): JSON line           │
│        with peer_pid / peer_path / classification / outcome           │
│        → ~/.screencap/run/audit.log (0o600, append-only)             │
└──────────────────────────────────────────────────────────────────────┘

SECURITY.md (U1) documents:
  - boundary = EUID + filesystem perms
  - in-scope vs out-of-scope threats
  - rationale for rejecting bearer token and provenance-gate alternatives
README.md (U4) and CLAUDE.md (U4) link to SECURITY.md so contributors find it.
```

---

## Implementation Units

### U1. SECURITY.md threat-model artifact

**Goal:** Publish the trust-boundary decision as a discoverable public artifact at the repo root.

**Requirements:** R1, R2

**Dependencies:** None

**Files:**
- Create: `SECURITY.md`

**Approach:**
- Sections to include: (a) Trust Boundary — EUID + filesystem perms, with explicit reference to `~/.screencap/run/` perm model; (b) In-scope threats (cross-user shared Mac, accidental umask drift, PID-spoofing of advisory metadata); (c) Out-of-scope threats (malware already running as the user, root, admin, physical access, backups); (d) Alternatives considered with rejection rationale, linking to this plan; (e) Reporting vulnerabilities (email or GitHub security advisory link).
- Voice: prose, not bulleted spec. The audience is downstream maintainers, security researchers, and end users — not the implementing engineer.
- Cite Apple Platform Security Guide on user-account boundary; cite `ssh-agent` / `gpg-agent` / Docker as industry comparables.
- The "Reporting vulnerabilities" section keeps the doc useful as the GitHub-surfaced security policy. If the project doesn't yet have a vulnerability reporting address, default to "open a private security advisory" and flag in this plan's Open Questions for follow-up.

**Patterns to follow:**
- GitHub `SECURITY.md` conventions (visible in the repo's "Security" tab).
- Match the tone of `CLAUDE.md` — concise, factual, no marketing.

**Test scenarios:**
- Test expectation: none — pure documentation, no executable behavior.

**Verification:**
- File renders correctly on GitHub (preview locally with a markdown renderer; check that headings, blockquotes, and links render).
- All links resolve (no broken anchors to plan paths or Apple docs).
- GitHub repo "Security" tab surfaces the file after merge.

---

### U2. Bind-time socket permission verification

**Goal:** Catch umask drift, accidental `chmod`, or tampered installs by re-verifying the socket and parent-directory permissions at bind time and aborting daemon startup on drift.

**Requirements:** R3

**Dependencies:** None

**Files:**
- Modify: `src/screencap/daemon/socket.py`
- Test: `tests/daemon/test_socket.py`

**Approach:**
- After `os.chmod(socket_path, 0o600)` and before `listener.listen()` in `bind_unix_socket`, stat both the parent dir (`socket_path.parent`) and the socket file. Verify mode bits match the expected `0o700` and `0o600` respectively. On drift, close the listener, unlink the socket, raise a new `DaemonSocketError` subclass with a clear message naming the offender (`parent dir 0o755 expected 0o700` etc.).
- Reuse the existing `DaemonSocketError` base class (no new error envelope; this is a startup-time abort, not an API error). Choose a name in the same family (e.g., `SocketPermsDrift`).
- Daemon startup path: `serve()` already catches `DaemonSocketError` subclasses and exits with code 1. Verify the new subclass surfaces a clean error line to stderr.
- Optionally also verify UID/GID ownership of both — defer that decision to implementation per Open Questions; default to mode-only.

**Execution note:** Add the bind-time perm test in `tests/daemon/test_socket.py` first (TDD red), then implement. The test surface is well-defined and the implementation is mechanical.

**Patterns to follow:**
- Existing `RogueFileAtSocketPath` (lines 38-39, raised from `_probe_existing_socket`) is the precedent for "daemon refuses to bind on suspicious filesystem state."
- `cleanup_socket` is idempotent — call it on the failure path so a half-bound socket doesn't linger.

**Test scenarios:**
- Happy path: bind-then-verify passes when both perms are correct. Daemon startup proceeds normally.
- Edge case: parent dir perms drift (e.g., chmod 0o755 after creation). Bind raises `SocketPermsDrift`; daemon does not start; stderr message names the offender.
- Edge case: socket file perms drift (e.g., chmod 0o644 after bind). Same shape — raises, does not listen.
- Edge case: parent dir is missing entirely (cleaned out between `_ensure_socket_directory` and the verify step). Raises with a clear message rather than a bare `FileNotFoundError`.
- Integration: full `screencap serve` subprocess via `serve_process` fixture exits non-zero with the expected stderr line when the test deliberately chmods the parent dir 0o755 after spawn.

**Verification:**
- All scenarios above pass.
- `pytest tests/daemon/test_socket.py` is green.
- Manual: `chmod 755 ~/.screencap/run` then `screencap serve` (a clean repro of the drift scenario) exits non-zero with the named error.

---

### U3. Peer-provenance audit log for privileged verbs

**Goal:** Persist a per-call audit record (timestamp, verb, peer PID, peer binary path, classification, outcome) for `/v0/recording.start` and `/v0/recording.stop` so operators have forensic capability under the same-UID trust model.

**Requirements:** R4

**Dependencies:** None (independent of U1, U2)

**Files:**
- Create: `src/screencap/daemon/audit_log.py`
- Modify: `src/screencap/daemon/app.py` (call audit hook from `recording_start` / `recording_stop` after the supervisor verb resolves)
- Test: `tests/daemon/test_audit_log.py` (new)
- Test: extend `tests/daemon/test_recording_start_provenance.py` if convenient for cross-cutting verification, otherwise keep new tests isolated

**Approach:**
- `audit_log.py` exposes `record_verb(verb: str, *, peer_pid: int | None, peer_path: str | None, classification: str, outcome: str, **extra)`. Resolves audit-log path via `Path.home() / ".screencap" / "run" / "audit.log"` (mirroring `_AUTO_LOG_PATH` in `_autospawn.py`).
- Writes a single JSON line per call. Schema: timestamp (ISO 8601), verb, peer_pid, peer_path, classification (`swiftui`/`cli`/`mcp`/`unknown` from `provenance.STARTED_BY_*`), outcome (`ok`/`error`/`<error_code>`), plus any verb-specific extras (e.g., recording name on start).
- Best-effort: wrap the file write in `try/except OSError` and log a `logger.warning` on failure. Audit-log unavailability MUST NOT cause the underlying verb to fail.
- File mode: open with `os.open(..., O_APPEND | O_CREAT | O_WRONLY, 0o600)` so first-write creates the file at 0o600. Parent dir is already 0o700 via `bind_unix_socket`.
- Integration: `recording_start` and `recording_stop` in `src/screencap/daemon/app.py` call `audit_log.record_verb(...)` on both the success path (after `supervisor.spawn`/`stop` returns) and the error path (in the `except DaemonAPIError` and `except Exception` branches). Peer info comes from the already-derived `derive_started_by_from_asgi_scope`-style helpers — refactor `provenance.py` to expose a richer "peer descriptor" if cleaner, or reach into the request scope directly.
- Audit log lifecycle: append-only in v1. No rotation, no shipping. Document the deferral in `## Scope Boundaries`.

**Execution note:** Implement the audit writer in `audit_log.py` test-first (file mode, JSON line shape, best-effort error handling). The route-handler integration in `app.py` is a thin call site.

**Patterns to follow:**
- `_AUTO_LOG_PATH` in `src/screencap/cli/_autospawn.py` — same path resolution shape (`Path.home() / ".screencap" / "run" / ...`).
- `provenance.derive_started_by_from_asgi_scope` — already extracts the peer PID + classification; reuse rather than reimplement.
- Existing daemon-side `logger = logging.getLogger(__name__)` pattern for any `logger.warning` on audit-write failure.

**Test scenarios:**
- Happy path (start): `recording.start` succeeds; audit log contains one JSON line with `verb=recording.start`, `outcome=ok`, the test-driven `peer_pid` (monkeypatched), and the expected classification.
- Happy path (stop): same shape for `recording.stop`.
- Error path: `recording.start` fails with `LOCK_CONTENDED` (existing error); audit log contains a line with `outcome=lock_contended` (or `outcome=error` + `error_code=lock_contended` — pick one shape and stick with it).
- Error path: `recording.start` fails with an unhandled exception; audit log contains a line with `outcome=internal_error`.
- Edge case: audit log file is unwritable (parent dir made read-only mid-test). Verb still succeeds; `logger.warning` fires; no exception propagates to the response.
- Edge case: audit log file does not exist on first call. `record_verb` creates it at mode 0o600.
- Edge case: peer info is unavailable (`derive_started_by` returns `unknown` / `peer_pid` is None). Line still written with `classification=unknown` and `peer_pid=null`.
- Integration: through the `uds_client_factory` fixture, POST to `/v0/recording.start` and `/v0/recording.stop`; verify both produce audit lines without breaking the existing recording-start provenance assertions in `test_recording_start_provenance.py`.
- Read-only verbs not audited: `recording.list`, `session.snapshot`, `daemon.info`, `events` produce no audit log entry. (Important — verifies scope of audit coverage matches the plan.)

**Verification:**
- All scenarios above pass.
- `pytest tests/daemon/test_audit_log.py` and `pytest tests/daemon/test_recording_start_provenance.py` are green.
- Manual: start and stop a recording via the Swift app and via `screencap start`; inspect `~/.screencap/run/audit.log` to see both classification values (`swiftui` vs `cli`) and `outcome=ok` lines.

---

### U4. README + CLAUDE.md pointers to SECURITY.md

**Goal:** Make `SECURITY.md` discoverable for contributors who land in `README.md` or `CLAUDE.md` first, without duplicating the threat model.

**Requirements:** R2

**Dependencies:** U1

**Files:**
- Modify: `README.md`
- Modify: `CLAUDE.md`

**Approach:**
- `README.md`: add a brief "Security" section near the bottom (one paragraph + link to `SECURITY.md`). No threat-model details — the link is the surface.
- `CLAUDE.md`: add a one-line entry under "Documented Solutions" (or a new "Security" subsection) noting that `SECURITY.md` documents the daemon socket trust boundary and is the source of truth for threat-model questions. Phrase as orientation for contributors, not as a rule.
- Do not edit any user-facing CLI help text in this unit (defer to a follow-up if the help text needs to mention security).

**Patterns to follow:**
- Existing CLAUDE.md "Documented Solutions" pointer to `docs/solutions/` is the right shape — short, factual, link-only.

**Test scenarios:**
- Test expectation: none — pure documentation linking, no executable behavior.

**Verification:**
- Both files render correctly.
- Links to `SECURITY.md` resolve.
- A reviewer reading `README.md` cold can find the threat model in one click.

---

## System-Wide Impact

- **Interaction graph:** `recording.start` and `recording.stop` route handlers acquire a new fire-and-forget side effect (audit-log write). No new dependencies on supervisor, event bus, or pidfile. Read-only verbs are unaffected. `/v0/daemon.info` stays unaffected so the post-install verifier (`launchagent.py::_daemon_info_responds`) and the auto-spawn smoke probe (`_autospawn.py`) keep working.
- **Error propagation:** Audit-log write failures MUST NOT propagate to the verb response — caller continues to get the normal envelope. Permission-drift at bind time DOES propagate as a hard startup failure (daemon does not serve). Both behaviors are intentional and documented in U2/U3.
- **State lifecycle risks:** Audit log is append-only with no concurrency lock. Single daemon process serializes all writes through asyncio; multiple parallel verb invocations are already serialized by `Supervisor`. No cross-process audit-log writer exists. Risk: if a future test or tool opens the file with truncation, data loss. Mitigate by always opening with `O_APPEND` and documenting the contract at the top of `audit_log.py`.
- **API surface parity:** No client-side change. Swift `DaemonClient`, CLI `_daemon_client.py`, and the future MCP server inherit the boundary unchanged. Schema versions in `src/screencap/daemon/schema.py` do NOT bump — no wire-format change.
- **Integration coverage:** Cross-layer scenarios that unit tests alone won't prove — (a) `screencap serve --install` + `_daemon_info_responds` post-install verifier still works (R6); (b) `screencap start` via the auto-spawn path still works in the F3 install case; (c) Swift app's `DaemonClient` still recording.start succeeds end-to-end after the audit-log integration. U3's verification step covers (c) manually; (a) and (b) are covered by existing `tests/daemon/test_launchagent.py` and `tests/daemon/test_idle_shutdown.py` regression tests staying green.
- **Unchanged invariants:** `PeerCheckingUnixSocket.accept()`'s EUID gate is unchanged. The error envelope module (`errors.py`) gets no edits. Schema versions stay the same. The CLI auto-spawn env-strip filter at `_autospawn.py:220-224` is untouched (no `SCREENCAP_DAEMON_*` env var is introduced). The Swift `DaemonClient` is untouched.

---

## Risks & Dependencies

| Risk | Likelihood | Impact | Mitigation |
|------|-----------|--------|------------|
| Audit-log writes block the asyncio event loop on slow disk (e.g., spinning rust, full disk, network-mounted home dir) | Low | Medium | Open the file with `O_APPEND` so individual writes are short; if profiling shows latency, move the write to `asyncio.to_thread` (deferred to implementation if measured). |
| Bind-time perm verification false-positives on macOS file-system edge cases (ACLs, restricted folders) | Low | High (daemon won't start) | Verify mode bits only by default; explicitly do NOT check ACLs in v1. If a real install hits a false positive, fall back to "warn + continue" behind a config flag (deferred unless seen in practice). |
| Audit log grows unbounded over weeks of heavy recording activity | Medium | Low | Document the deferred rotation work in `## Scope Boundaries`; if it becomes a real operational issue, follow up with a logrotate-style policy in a separate ticket. v1 lines are ~200 bytes — even 100 recordings/day takes years to hit a problem. |
| `SECURITY.md` rejected by a reviewer who wants Option C (bearer token) anyway | Low | Medium | The Alternatives section in this plan and in `SECURITY.md` itself names the rejection rationale with citations to Phase 1/2 plans and external prior art. If the reviewer still wants Option C, that's a real product conversation, not a doc fix — surface back to the user. |
| Audit-log file mode race: another process creates `audit.log` at 0o644 before the daemon's first write | Very low (parent dir is 0o700, so only same-UID can do this) | Low | First-write uses `os.open(..., O_CREAT, 0o600)` AND a defensive `os.chmod(0o600)` after open (idempotent if already 0o600). Document the contract. |
| Audit-log emission inadvertently leaks sensitive metadata (e.g., recording names containing PII) | Low | Medium | The audit line includes verb + peer + outcome by design; recording names are already in the request body and elsewhere on disk. Treat the audit log as same sensitivity class as `~/.screencap/recordings/`. No additional PII review needed for v1. |

---

## Alternative Approaches Considered

This section was an explicit user-requested deliverable: compare all three SCR-64-named approaches, recommend one, and document the rejection rationale for the others.

### Option A (recommended): Accept same-UID + document + harden hygiene

**What:** Status quo (`getpeereid()` at accept-time + 0o700 dir + 0o600 socket), plus a public `SECURITY.md`, plus bind-time perm verification, plus peer-provenance audit log.

**Why chosen:**
- Matches every comparable tool's posture: `ssh-agent`, `gpg-agent`, Docker (group-equivalent), 1Password CLI (Keychain via desktop helper), `gh` (Keychain-backed). All ship same-UID trust as the boundary for local-socket IPC.
- Aligns with the explicit positions in `docs/plans/2026-05-08-001-feat-daemon-architecture-phase-1-plan.md` line 58 and `docs/plans/2026-05-08-002-feat-daemon-architecture-phase-2-plan.md` line 66, which already named filesystem perms as the v1 authorization surface.
- Aligns with Apple Platform Security Guide framing (user account = primary data-protection boundary).
- Zero behavior change on the happy path. No risk to the F3 auto-spawn path, the Swift `DaemonClient`, or any existing test.
- Forensic-capability gap (the actual concrete weakness when same-UID is the boundary) is addressed by the audit log without introducing a new auth surface.

**Limitations:**
- Does not raise the bar against a same-UID attacker (by design — that's the documented boundary). Mitigations: TCC still gates screen capture itself; `~/.screencap/run/` is 0o700; audit log gives forensic trail.

### Option B (rejected): Promote provenance to an enforcement gate

**What:** Elevate `derive_started_by_from_asgi_scope` from advisory metadata into an allowlist gate on `/v0/recording.start` and `/v0/recording.stop`. Reject `unknown` callers with a new `forbidden` error code + HTTP 403. Allowlist: known SwiftUI bundle path, known CLI/MCP argv shapes.

**Why rejected:**
- **PID-reuse / TOCTOU race is unavoidable on AF_UNIX without code-signing checks.** [Scott Knight (2020)](https://knight.sc/reverse%20engineering/2020/03/20/audit-tokens-explained.html) and the [Quarkslab Intego writeup](https://blog.quarkslab.com/intego_lpe_macos_2.html) document the canonical exploit: a malicious process sends the request, then `posix_spawn`s a trusted binary which gets the same PID before the server checks. `LOCAL_PEEREPID` does not carry a generation number; only the audit-token's `p_idversion` does, and there is no public `getsockopt` option to extract an audit token on AF_UNIX.
- **Phase 1 plan line 73 already predicted this and Phase 2 plan line 669 already rejected it:** "Promoting peer-PID to auth would require addressing PID reuse / TOCTOU and would block on a credential model not yet designed."
- **Dev-mode ad-hoc-signed builds and any non-bundled binary would fail the allowlist** (see `docs/solutions/build-errors/macos-ad-hoc-signing-tcc-rebuild-treadmill.md`). The plan would constantly break developers running locally-built CLIs or the source-mode `script/build_and_run.sh` helper.
- **Locks out power-user automation that runs as the same user** (cron, ad-hoc scripts, future MCP-style tools that don't yet exist).
- **The threat it defends against** ("a same-UID process that isn't a known ScreenCap binary calls `/v0/recording.start`") is already trivially defeated by the attacker invoking the bundled CLI binary directly. The gate provides the illusion of a boundary without the substance.

**What it would take to revisit:** A move to a single signed-binary distribution model + adopting `SecCodeCopyGuestWithAttributes(kSecGuestAttributePid)` with explicit acknowledgement of the residual TOCTOU window. Or migrating from AF_UNIX to XPC (Apple's recommended mechanism for code-signing-verified peer auth).

### Option C (rejected): Bearer token co-installed via SMAppService

**What:** Daemon generates a random secret on first launch, writes it to `~/.screencap/run/api.token` (mode 0o600); Swift `DaemonClient`, CLI `_daemon_client.py`, and any future MCP client read the file and send it as an `Authorization: Bearer <token>` header. Daemon middleware checks `hmac.compare_digest` against the live token and 401s on mismatch.

**Why rejected:**
- **A 0o600 token file accessible to the same UID provides no meaningful defense against the threat model it implies.** Any process that can `connect()` to the socket (must be same-UID, because parent dir is 0o700) can also `open()` the token file (same-UID, same dir, same mode). The bearer token adds a `read(2)` to the attacker's exploit chain and nothing else. This is the explicit Apple DTS guidance and the documented `ssh-agent` design rationale.
- **Worse-than-no-token failure modes are real and observed in the wild:** developers relax token-file perms to debug ("just chmod 644 for a sec"); tokens get logged in shell history (`screencap --token=…`); tokens appear in `ps` output (CLI args); tokens leak into crash reports and support bundles. The bare-socket model has none of these surfaces.
- **Bootstrapping complexity is non-trivial.** CLI auto-spawn (`src/screencap/cli/_autospawn.py`) strips all `SCREENCAP_DAEMON_*` env vars before `posix_spawn` to defeat env-injection attacks. A token approach must coordinate via filesystem (daemon writes, clients read), which means the auto-spawn client must wait for the token file to appear before sending the first request. New race conditions, new edge cases, new test surfaces.
- **The Swift `DaemonClient` and CLI `_daemon_client.py` both need new auth-aware code paths.** R5 explicitly excludes this complexity from scope.
- **Phase 2 plan implicitly deferred this on the same grounds:** "Promoting peer-PID to auth would require ... a credential model not yet designed." A bearer token IS a credential model — and the design pass would not produce a different conclusion than "this doesn't address the threat."

**What it would take to revisit:** A concrete threat scenario where some classes of same-UID processes legitimately should not be able to call the daemon but cannot be excluded by filesystem permissions. We don't have one. If we did, the right answer would be code-signing verification (Option C-prime), not a bearer token.

### Option C-prime (out of scope, named for completeness)

**What:** Per-call code-signing verification via `SecCodeCopyGuestWithAttributes(kSecGuestAttributePid)` + `SecCodeCheckValidityWithErrors` against a Team-ID / bundle-identifier requirement string.

**Why out of scope for this plan:** Genuinely raises the bar against a same-UID attacker (forcing a Developer ID signature forgery, which requires Apple's private key). But it presupposes a single-signed-binary distribution model that ScreenCap doesn't have today (CLI may be unsigned or ad-hoc-signed via Homebrew, source builds, dev rebuilds). The mixed distribution model would either break developer workflows or require maintaining per-distribution-channel requirement strings, both of which are substantial design questions beyond SCR-64's scope.

---

## Success Metrics

- `SECURITY.md` is published, GitHub-surfaced in the repo's "Security" tab, and findable from `README.md` and `CLAUDE.md`.
- A new contributor reading the codebase cold can answer "what's the daemon socket's trust boundary, and why?" without grep-ing through Phase 1/2 plan archaeology.
- `pytest tests/daemon/` is green on macOS with no regressions in `test_launchagent.py`, `test_idle_shutdown.py`, or `test_recording_start_provenance.py`.
- A manual recording via the Swift app produces one `recording.start` and one `recording.stop` audit-log line; same via `screencap start` / `screencap stop`. Both correctly classify the caller.
- `screencap serve --install` post-install verifier and `screencap start` auto-spawn path both still succeed (no regression in F3 install or cron-driven `screencap status`).

---

## Documentation Plan

- `SECURITY.md` is the primary deliverable (U1).
- `README.md` gets a short "Security" section linking to `SECURITY.md` (U4).
- `CLAUDE.md` gets a one-line pointer (U4).
- After merge, plan a follow-up `/ce-compound` run to capture the trust-boundary decision as institutional knowledge in `docs/solutions/security/`.

---

## Sources & References

- **Linear ticket:** [SCR-64](https://linear.app/zk-email/issue/SCR-64/daemon-socket-has-no-per-caller-auth-pr-180-made-daemon-side-the-sole)
- **Upstream PR:** [proteus-computer-use/screencap#180](https://github.com/proteus-computer-use/screencap/pull/180)
- **Related design:** `docs/superpowers/specs/2026-05-15-scr-54-macos-permission-ownership-design.md`
- **Prior daemon plans:** `docs/plans/2026-05-08-001-feat-daemon-architecture-phase-1-plan.md`, `docs/plans/2026-05-08-002-feat-daemon-architecture-phase-2-plan.md`
- **Relevant code:** `src/screencap/daemon/socket.py`, `src/screencap/daemon/app.py`, `src/screencap/daemon/provenance.py`, `src/screencap/daemon/launchagent.py`, `src/screencap/cli/_autospawn.py`, `src/screencap/cli/_daemon_client.py`, `macos/ScreenCap/Controllers/DaemonClient.swift`
- **Institutional learnings:** `docs/solutions/runtime-errors/eventbus-late-listener-replay-2026-05-12.md`, `docs/solutions/runtime-errors/launchd-plist-tilde-expansion-2026-05-09.md`, `docs/solutions/build-errors/macos-ad-hoc-signing-tcc-rebuild-treadmill.md`, `docs/solutions/integration-issues/macos-foundation-process-pipe-pitfalls.md`
- **Apple primary sources:** Apple Platform Security Guide (March 2026 edition); [Apple DevForums thread 74498](https://developer.apple.com/forums/thread/74498); [TN3127 — Inside Code Signing: Requirements](https://developer.apple.com/documentation/technotes/tn3127-inside-code-signing-requirements); [xpc_connection_set_peer_code_signing_requirement docs](https://developer.apple.com/documentation/xpc/3755524-xpc_connection_set_peer_code_sig/)
- **PID-reuse exploit references:** [Scott Knight — Audit tokens explained](https://knight.sc/reverse%20engineering/2020/03/20/audit-tokens-explained.html); [Quarkslab — Intego LPE writeup](https://blog.quarkslab.com/intego_lpe_macos_2.html)
- **Industry comparables:** [Docker — Protect the daemon socket](https://docs.docker.com/engine/security/protect-access/); [Wikipedia — ssh-agent](https://en.wikipedia.org/wiki/Ssh-agent); [1Password — Authenticate any CLI](https://developer.1password.com/docs/cli/authenticate-clis/)
