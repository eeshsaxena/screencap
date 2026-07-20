---
title: "fix: High-priority daemon tickets — EventBus replay, stop() TOCTOU, rogue-bind exit code, stderr backpressure"
type: fix
status: completed
date: 2026-05-09
---

# fix: High-priority daemon tickets — EventBus replay, stop() TOCTOU, rogue-bind exit code, stderr backpressure

## Summary

Land four high-priority bug fixes against the Phase 1 daemon. A single bounded EventBus replay buffer (last-N stamped events, retained in-memory) closes two distinct late-subscriber races: the SwiftUI shell never observing `recording_started` because it subscribes after the daemon's own internal subscription burned the event (TKT-B), and `Supervisor.stop()` blocking for the full 30 s timeout because `_exit_poll` published `recording_finalized` in the await gap before `stop()`'s subscription existed (TKT-A). On the install path, a rogue same-EUID process holding `~/.screencap/run/api.sock` now causes the daemon to exit with `EX_TEMPFAIL=75` and log the offending PID via `lsof`, with `InstallResult.state = "install_failed_already_running"` for the CLI install verifier (TKT-C). On the engine ↔ supervisor stderr channel, the kernel pipe is widened to 1 MiB via `F_SETPIPE_SZ` to push out the point at which a slow `_stderr_pump` drain blocks the engine's `sys.stderr.write`, with measurement-driven follow-ups deferred (TKT-D).

---

## Problem Frame

Phase 1 shipped the launchd → daemon → engine subprocess topology with an in-process EventBus that intentionally has no replay buffer. Tier-2 and Tier-3 code reviews then surfaced four real bugs that share a common shape: producers and consumers race across `await` points, and the bus's "live-only" contract makes the races fatal instead of recoverable. Two of the four (TKT-A, TKT-B) are direct consequences of the no-replay design and have already burned the SwiftUI happy path — the recorder gets stuck in `.starting` because the production timing is opposite of what the unit tests exercise. One (TKT-C) is a launchctl-loop diagnostic gap: when a rogue or stale daemon already holds the socket, the operator sees a generic `last exit code = 1` and a slow restart loop, with no path to the offending PID. The fourth (TKT-D) is a kernel-pipe ceiling: a slow `_stderr_pump` drain transitively blocks the engine's lifecycle event emission, and Phase 2's MCP subscriber will only push the system closer to that ceiling.

The ce-brainstorm origin doc explicitly deferred replay to "if real consumers need historical replay" ([Phase 1 plan](docs/plans/2026-05-08-001-feat-daemon-architecture-phase-1-plan.md), Deferred section). TKT-A and TKT-B are those consumers.

---

## Requirements

- R1. `Supervisor.stop()` must return within 1 s when `_exit_poll` publishes `recording_finalized` in the await gap between `is_alive()` and the stop subscription, instead of blocking for the full 30 s `stop_timeout` (TKT-A acceptance).
- R2. A SwiftUI client that subscribes to `/v0/events?since=<recording_start.cursor>` after `recording.start` returns must observe the `recording_started` event and transition `RecordingState` from `.starting` to `.recording` (TKT-B acceptance).
- R3. When a same-EUID rogue process holds `~/.screencap/run/api.sock`, the daemon process must exit with code `75` (`EX_TEMPFAIL` from `sysexits.h`) and log the offending PID once at the failed-bind moment (TKT-C acceptance, item 1 + 2).
- R4. `screencap serve --install`'s `InstallResult.state` must distinguish `install_failed_already_running` from `install_failed_daemon_did_not_start` (TKT-C acceptance, item 3).
- R5. The engine's `sys.stderr.write` must not block when the daemon's `_stderr_pump` is briefly slow; at minimum the kernel stderr pipe is widened to 1 MiB via `F_SETPIPE_SZ` and the chosen size is documented (TKT-D acceptance).
- R6. `/v0/events?since=N` for `N` older than the retained replay window must return the existing `cursor_unknown` envelope with HTTP 410, finally honoring the contract the Phase 1 plan promised but the current code accepts silently.
- R7. Existing daemon and SwiftUI integration tests continue to pass; new regression tests deterministically exercise each of the four races above.

**Origin acceptance examples:** AE-A (TKT-A), AE-B (TKT-B), AE-C (TKT-C), AE-D (TKT-D), each carried verbatim from the corresponding ticket's `## Acceptance` section.

---

## Scope Boundaries

- The 6 medium-priority tickets in `docs/tickets/` (notably `2026-05-08-engine-topology-spike.md`, `2026-05-08-fix-recorder-cursor-advancement.md`, `2026-05-08-fix-recorder-task-handles.md`, `2026-05-08-fix-daemon-started-at-import-time.md`, `2026-05-08-refactor-errors-dual-track-delete.md`, `2026-05-09-fix-pid-identity-check-on-terminate.md`).
- Phase 2 work — engine consolidation into the daemon, MCP server, CLI as API client.
- Time-based replay-buffer eviction. The buffer is bounded by event count only.
- Coalescing strategies for high-frequency events (e.g., `frame_written`). Replay retains exactly what the bus published, in order.
- Reworking the `_stderr_pump`/`EventBus.publish` lock semantics. The TKT-D ticket's claim that publish `await`s on `put` is incorrect against current code (publish uses `put_nowait`); this plan calls that out and limits the fix to kernel-pipe widening per the user-confirmed scope.
- Changes to the SwiftUI `DaemonInstallController` / SMAppService path on TKT-C. SMAppService reports its own NSError codes and cannot observe daemon process exit; `install_failed_already_running` is surfaced only via the CLI install verifier path.

### Deferred to Follow-Up Work

- TKT-D approach (2): decouple bridge from publish via an internal asyncio queue + drop policy. Defer until measurement under realistic load shows F_SETPIPE_SZ alone is insufficient. Track as a follow-up ticket if the regression test from U6 begins to flake or production traces show pump stalls.
- TKT-D approach (3): off-stderr dedicated `os.pipe()` channel for engine→supervisor lifecycle events. Defer until the engine-topology spike (`docs/tickets/2026-05-08-engine-topology-spike.md`) lands; if the spike picks "in-process engine," approach (3) becomes moot.
- AST-based pinning test for TKT-A (asserting `bus.subscribe` appears before `is_alive` in `Supervisor.stop` source). Considered but not adopted: the behavioral regression test in U3 fails deterministically before the fix, which is sufficient. Worth revisiting if the same race re-appears under a different shape (the SIGINT handler bug had four iterations historically — see `docs/solutions/runtime-errors/sigint-handler-timing-and-recording-stop-methods.md`).
- Correcting the TKT-D ticket body to amend the "publish awaits put" mis-statement. Track as a docs PR after this lands.

---

## Context & Research

### Relevant Code and Patterns

- [src/screencap/daemon/event_bus.py](src/screencap/daemon/event_bus.py) — `_Subscription` already has `cursor_at_subscribe`; `publish()` uses `put_nowait` and closes overflowed subscribers with `slow_consumer`. Replay buffer lands here.
- [src/screencap/daemon/supervisor.py:265-344](src/screencap/daemon/supervisor.py:265) — `Supervisor.stop()` flow with the TOCTOU at line 306 (`is_alive()`) → line 318 (`bus.subscribe()`).
- [src/screencap/daemon/supervisor.py:213-263](src/screencap/daemon/supervisor.py:213) — `Supervisor.spawn()` already captures `start_cursor = self._bus.current_cursor()` at line 217 BEFORE Popen and returns it as `RecordingStartResponse.cursor`. The wire contract is already correct; only the daemon's `?since=` replay needs to honor it.
- [src/screencap/daemon/supervisor.py:430-447](src/screencap/daemon/supervisor.py:430) — `_stderr_pump` uses `await asyncio.to_thread(stderr.readline)`; `_exit_poll` is the 1 Hz lifecycle watcher.
- [src/screencap/daemon/app.py:264-347](src/screencap/daemon/app.py:264) — `events_stream` route, `?since=` parsing at lines 266-292, `subscribed` frame at line 302-309. Replay events are yielded between the `subscribed` frame and the `while True: sub.queue.get(...)` loop.
- [src/screencap/daemon/server.py:124-127](src/screencap/daemon/server.py:124) — `DaemonAlreadyRunning` catch site; today returns exit code 1.
- [src/screencap/daemon/socket.py:65-148](src/screencap/daemon/socket.py:65) — `_probe_existing_socket` and `bind_unix_socket`. lsof PID capture lands inside `_probe_existing_socket` after `connect()` succeeds; PID flows up via a new `existing_pid: int | None` attribute on `DaemonAlreadyRunning`.
- [src/screencap/daemon/launchagent.py:26-31](src/screencap/daemon/launchagent.py:26) — `InstallResult` dataclass; `state` is a free-form string. Existing values include `installed_and_running`, `install_failed_daemon_did_not_start`, etc. Add `install_failed_already_running`.
- [macos/Screencap/Controllers/RecorderController.swift:565-621](macos/Screencap/Controllers/RecorderController.swift:565) — `consumeDaemonEvents`. Already passes `snapshot.cursor` to `daemon.subscribe(sinceCursor:)`. No client change needed once daemon honors replay.
- [macos/Screencap/Controllers/DaemonClient.swift:140-153, 226-305](macos/Screencap/Controllers/DaemonClient.swift:140) — `RecordingStartResponse.cursor` and `subscribe(sinceCursor:)`.
- [macos/ScreencapTests/RecorderControllerDaemonTests.swift:25-56](macos/ScreencapTests/RecorderControllerDaemonTests.swift:25) — current happy-path test; the mock delivers `started` AFTER the subscribe, masking the production race. The fix must add a test that delivers `started` BEFORE the subscribe and asserts the recorder still transitions.
- [tests/daemon/test_event_bus.py](tests/daemon/test_event_bus.py) — slow-consumer test at line 62-78 confirms `publish()` is non-blocking. Pure-unit pattern for the replay-buffer additions.
- [tests/daemon/test_supervisor.py:36-103](tests/daemon/test_supervisor.py:36) — fake-engine-script fixture pattern. Extend with `FAKE_FINALIZE_BEFORE_TERM=1` env var to deterministically drive the TKT-A race.
- [tests/daemon/test_control_verbs.py:134-196](tests/daemon/test_control_verbs.py:134) — HTTP-over-UDS pattern via `_serve` ctx; home for the wire-level replay regression test.
- [tests/test_serve_command.py:62-89](tests/test_serve_command.py:62) — "second serve fails" pattern; home for the TKT-C exit-code-75 regression.
- [scripts/mcp_contract_smoke.py:60](scripts/mcp_contract_smoke.py:60) — already on the cursor contract via `?since={cursor}`. Validates the contract end-to-end after replay lands.

### Institutional Learnings

- [docs/solutions/runtime-errors/sigint-handler-timing-and-recording-stop-methods.md](docs/solutions/runtime-errors/sigint-handler-timing-and-recording-stop-methods.md) — Same archetypal bug as TKT-A. The fix pattern ("install handler BEFORE the long-running setup, with None guards") translates directly to "subscribe to the bus BEFORE the `is_alive()` check, with replay covering anything published in the gap." The doc also documents 4 historical iterations of the same shape — strong signal that late-listener races are a recurring class in this codebase.
- [docs/solutions/integration-issues/macos-foundation-process-pipe-pitfalls.md](docs/solutions/integration-issues/macos-foundation-process-pipe-pitfalls.md) — Pitfall 1 (~64 KB default kernel pipe blocks the writer) is the exact failure mode TKT-D addresses. The Swift-side fix was `readToEnd()` after nilling the readability handler; the Python analog is "F_SETPIPE_SZ buys time, but pair with a guaranteed drain on shutdown." Also flags that the macOS F_SETPIPE_SZ ceiling is under-documented and worth empirical verification.
- [docs/solutions/runtime-errors/launchd-plist-tilde-expansion-2026-05-09.md](docs/solutions/runtime-errors/launchd-plist-tilde-expansion-2026-05-09.md) — Establishes the precedent for using `sysexits.h` codes (it used `EX_CONFIG=78`); informs the choice of `EX_TEMPFAIL=75` for TKT-C and what `launchctl print gui/$UID/<label>` field-level checks the install verifier should perform. Also: "`launchctl bootstrap` returning success while the daemon never actually starts is a known false-positive — verify socket connectivity, not just bootstrap return code."
- [docs/solutions/runtime-errors/chunk-upload-sentinel-gating-and-data-loss.md](docs/solutions/runtime-errors/chunk-upload-sentinel-gating-and-data-loss.md) — Tangential. The "incomplete state treated as authoritative" / "track the full universe of expected work" principle applies to replay-buffer design: subscribers retrieve events by monotonic stamp, not by subscription-time arrival. Use the existing `cursor` stamps and require subscribers to supply `since=`; dedupe is the subscriber's responsibility.

---

## Key Technical Decisions

- **Bounded replay buffer keyed by `cursor`, retained in-memory in `EventBus` itself.** Capacity = 256 events. Rationale: TKT-A's gap is sub-100 ms (single asyncio yield); TKT-B's gap is ≤ a UDS HTTP round-trip (typically <100 ms). 256 events covers a worst-case burst (e.g., a flurry of `frame_written`) without unbounded memory. Per-subscriber `QUEUE_MAXSIZE = 1024` remains the live cap; replay ring at 256 is a deliberate fraction (you'd rather drop very-late subscribers cleanly with `cursor_unknown` than paper over hour-long disconnects).
- **No time-based eviction.** A monotonic event-count cap is simpler, easier to test, and aligned with the `cursor` stamp semantics. Time-based eviction adds clock-skew failure modes for no real consumer benefit.
- **`/v0/events?since=N` semantics finalize:** `N > current_cursor` → 410 `cursor_unknown` (unchanged); `current_cursor − retained_window ≤ N ≤ current_cursor` → replay matching events in ascending cursor order, then enter live loop; `N < current_cursor − retained_window` → 410 `cursor_unknown` (NEW — currently silently treated as "live from now" per `app.py:266-292`). The Phase 1 plan promised this behavior but the code took the more permissive path; this plan finally aligns them.
- **TKT-A fix relies on the replay buffer, not on swapping subscribe-vs-check order.** `Supervisor.stop()` keeps its current control flow; the late `bus.subscribe()` after `is_alive()` returning true now succeeds because the replay buffer surfaces the `recording_finalized` event the `_exit_poll` task may have published in the await gap. This avoids the "leaks subscriptions on early-return paths" risk the ticket flagged for the swap-order alternative.
- **TKT-C exit code is `75` (EX_TEMPFAIL).** Not `78` (EX_CONFIG, which the launchd-tilde fix used) — `EX_TEMPFAIL` is semantically correct ("temporary failure, try again") and lets an operator distinguish the two via `launchctl print` without rewriting tests of the EX_CONFIG path.
- **TKT-C lsof capture is best-effort and non-blocking.** `subprocess.run(["lsof", "-tU", str(path)], timeout=1.0)`; on failure, log "rogue PID unknown" and continue. Never let a diagnostic block daemon startup further. PID flows up via a new `existing_pid: int | None` attribute on `DaemonAlreadyRunning`.
- **TKT-D F_SETPIPE_SZ size = 1 MiB (`1 << 20`).** ~16× the typical ~64 KB macOS default, sufficient given Phase 1's 1–2 subscriber count, and well within the kernel ceiling (verified empirically in the unit test). Sized to be a clear "v1 safety net" not a permanent fix; if it's ever insufficient, that's the signal to revisit approach (2) from the ticket.
- **TKT-D rationale corrected from the ticket body.** The `EventBus.publish` claim about awaiting `put` per subscriber is wrong against current code (publish uses `put_nowait` and drops slow consumers). The real backpressure path is engine kernel-stderr → `_stderr_pump`'s `to_thread(stderr.readline)` throughput; F_SETPIPE_SZ widening is still the right v1 fix, but the rationale is "give the pump headroom against burst write rates" not "decouple from the bus." The fix in this plan does not depend on the ticket's incorrect mechanism.

---

## Open Questions

### Resolved During Planning

- **Replay buffer sizing.** Decided: 256 events, count-bounded, no time eviction. Rationale above.
- **Should `/v0/events?since=N` for N < retained-window return 410 `cursor_unknown`?** Decided: yes. Aligns with the Phase 1 plan's stated contract and the existing `since > current` 410 path.
- **Where does TKT-C's lsof PID capture live?** Decided: inside `_probe_existing_socket` in `socket.py`, after `connect()` succeeds. Best-effort, 1 s timeout.
- **TKT-A: replay buffer or subscribe-then-recheck?** Decided: replay buffer (see Key Technical Decisions). User confirmed scope.
- **TKT-B: synthesize state from response or replay?** Decided: replay. User confirmed scope.
- **TKT-D scope.** Decided: `F_SETPIPE_SZ` only (approach 1). User confirmed.
- **Schema version bump?** No. The replay behavior is monotonically additive for `?since=` callers — clients that already pass a valid cursor see strictly more correct behavior. The 410 for `since < current − retained_window` is a tightening of behavior that was already documented as intended by the Phase 1 plan but not implemented; this is bug-fix-aligned-with-plan, not a contract change.

### Deferred to Implementation

- **Exact F_SETPIPE_SZ ceiling on the target macOS versions.** Verify empirically in U6's unit test by attempting `1 << 20` and falling back to `1 << 17` (128 KiB) if the larger size is rejected with `EINVAL`. The fallback path is rare but not impossible; the test should assert the call returned a size > the default.
- **Whether `EVENT_RECORDING_FINALIZED` is the only event `_exit_poll` can publish in the TOCTOU window.** Audit during U3 implementation; expand the regression test to cover any other lifecycle event the poll task can emit between `is_alive()` and the late subscription.
- **Whether replay buffer size 256 is the right floor.** Resolve at U1 implementation by counting events emitted during a typical 10-second recording in production telemetry. If a single recording can plausibly emit > 200 events in a sub-second window, raise to 512.

---

## Implementation Units

### U1. EventBus bounded replay buffer

**Goal:** Add a count-bounded ring of recent stamped events to `EventBus`, exposed via a new `subscribe(since: int | None = None)` parameter that yields buffered events to a fresh subscription before live delivery starts.

**Requirements:** R1 (foundation), R2 (foundation), R7

**Dependencies:** None.

**Files:**
- Modify: `src/screencap/daemon/event_bus.py`
- Test: `tests/daemon/test_event_bus.py`

**Approach:**
- Add a `collections.deque(maxlen=REPLAY_BUFFER_SIZE)` to `EventBus`, populated inside `publish()` after the cursor stamp, before fan-out. `REPLAY_BUFFER_SIZE = 256` exposed as a module-level constant beside `QUEUE_MAXSIZE`.
- Extend `subscribe()` signature to `subscribe(since: int | None = None) -> _Subscription`. Behavior:
  - `since is None` → current behavior (live-only, `cursor_at_subscribe = self._cursor`).
  - `since > self._cursor` → raise a new `CursorUnknownError(cursor=since, current=self._cursor, oldest_retained=...)`.
  - `since < self._cursor − len(buffer)` (older than retained window) → raise `CursorUnknownError`.
  - Otherwise → enqueue all buffered events with `cursor > since` into the new subscription's queue (in ascending cursor order) before adding to `_subscribers`.
- `CursorUnknownError` is an in-module exception, mapped to the existing `cursor_unknown` HTTP 410 envelope at the `app.py` boundary. Do NOT re-raise into the live publish path.
- `current_cursor()` unchanged. Add `oldest_retained_cursor() -> int | None` for the `app.py` 410 path to surface in the error envelope.

**Test scenarios:**
- Happy path: publish 5 events, then `subscribe(since=2)` returns events 3, 4, 5 in order before any live-published event.
- Happy path: publish 5 events, then `subscribe(since=5)` returns no buffered events, then a live publish delivers event 6.
- Edge case: replay buffer at capacity (publish 300 events with `REPLAY_BUFFER_SIZE=256`); `subscribe(since=10)` raises `CursorUnknownError` because event 10 is no longer retained.
- Edge case: `subscribe(since=None)` matches the pre-change behavior (live-only); existing `test_event_bus.py` tests pass unchanged.
- Edge case: `subscribe(since=0)` on a fresh bus with `current_cursor=0` returns no buffered events and starts live (cursor 0 means "before any event"; valid).
- Edge case: `subscribe(since=current_cursor + 1)` raises `CursorUnknownError` (future cursor).
- Error path: replay events are independent dict copies — mutating a replayed event in the test does not affect a second subscriber's replay of the same cursor.
- Error path: `shutdown()` while replay is in-flight closes the new subscription with `SHUTDOWN`, not `slow_consumer`.

**Verification:**
- All existing `tests/daemon/test_event_bus.py` cases pass without modification.
- New test cases above pass deterministically.
- Memory bound: a long-running daemon's `EventBus` retains exactly `REPLAY_BUFFER_SIZE` events in the deque; verifiable by inspecting `len(bus._buffer)` after `> 256` publishes.

---

### U2. `/v0/events?since=N` honors replay; stale cursor returns 410

**Goal:** Wire U1's replay support into the HTTP `/v0/events` route so subscribers passing `?since=N` receive buffered events before the live loop starts, and stale cursors return the existing `cursor_unknown` envelope at HTTP 410 instead of being silently treated as "live from now."

**Requirements:** R2, R6, R7

**Dependencies:** U1.

**Files:**
- Modify: `src/screencap/daemon/app.py`
- Test: `tests/daemon/test_control_verbs.py`

**Approach:**
- Replace the current `since`-parsing block at `app.py:266-292` so it calls `bus.subscribe(since=since)` and catches `CursorUnknownError` from U1, returning the existing `cursor_unknown` envelope (HTTP 410) with `daemon_cursor` and `oldest_retained_cursor` fields populated from the error's attributes.
- The `subscribed` frame's `cursor` field continues to carry `sub.cursor_at_subscribe`. For replay subscriptions, this is the caller's `since` value (i.e., the replay starts immediately after that cursor).
- The replay events are already in `sub.queue` when the response loop starts (U1 enqueues them under the bus lock during `subscribe`), so the existing `while True: event = await sub.queue.get(...)` loop yields replay events first, naturally, before any live event lands. No reorder required.
- Reject `since` values that are non-integer or negative with the existing `invalid_cursor` 400 envelope (current behavior preserved).

**Test scenarios:**
- Happy path: HTTP `POST /v0/recording.start` returns `cursor=N`; subsequent `GET /v0/events?since=N` reads the `subscribed` frame followed by the `recording_started` event with `cursor > N`, end-to-end over UDS HTTP. **Covers AE-B.**
- Happy path: publish 3 events via the supervisor (e.g., a fake engine emitting test events); `GET /v0/events?since=current_cursor−2` replays the last 2, then receives a 4th live event.
- Edge case: `GET /v0/events?since=current_cursor+5` returns HTTP 410 with `cursor_unknown` envelope and `daemon_cursor` field equal to the actual current cursor.
- Edge case: `GET /v0/events?since=−1` returns HTTP 400 with `invalid_cursor` envelope.
- Edge case: `GET /v0/events?since=0` on a daemon that has just emitted 5 events streams those 5 events as replay before live (assuming all 5 are in the retained window).
- Error path: `GET /v0/events?since=` (empty) is treated as "no `since`" — live-only — matching current behavior (preserve).
- Integration scenario: two concurrent subscribers, one with `since=current−2` (gets 2 replay events) and one without (live-only); subsequent live publish reaches both.

**Verification:**
- `tests/daemon/test_control_verbs.py` test for the "start, then subscribe-after, observe started" flow passes.
- The cursor_unknown 410 path is exercised by a new test asserting the exact envelope shape.
- Existing `?since=` tests pass unchanged.

---

### U3. Fix `Supervisor.stop()` TOCTOU using replay buffer

**Goal:** Eliminate the race where `_exit_poll` publishes `recording_finalized` between the `is_alive()` check and `bus.subscribe()` in `Supervisor.stop()`. The fix uses U1's replay so the late subscription captures the missed event without changing the control-flow ordering of the early-return paths.

**Requirements:** R1, R7

**Dependencies:** U1.

**Files:**
- Modify: `src/screencap/daemon/supervisor.py`
- Test: `tests/daemon/test_supervisor.py` (add `FAKE_FINALIZE_BEFORE_TERM=1` fixture variant)
- Test: `tests/daemon/test_control_verbs.py` (wire-level race regression)

**Approach:**
- At `supervisor.py:306` (the `is_alive()` branch in `stop()`), capture `pre_check_cursor = self._bus.current_cursor()` BEFORE the `is_alive()` check.
- Replace the `final_sub = await self._bus.subscribe()` at line 318 with `final_sub = await self._bus.subscribe(since=pre_check_cursor)`. Any `recording_finalized` published by `_exit_poll` between the cursor capture and this subscribe lands in `final_sub.queue` via the replay path before live delivery resumes.
- `_wait_on_subscription` semantics unchanged — it consumes `final_sub.queue` until it sees `EVENT_RECORDING_FINALIZED` or the timeout fires.
- The existing `_exit_lock` + `_exit_handled` idempotency guards (`supervisor.py:151-152, 449-453`) keep the publish path single-shot, so replay never duplicates the event.
- Apply the same `pre_check_cursor` + `subscribe(since=…)` pattern to the parallel `Supervisor.shutdown()` flow at `supervisor.py:350+` for symmetry — same race shape, same fix.

**Test scenarios:**
- Happy path: normal `stop()` against a healthy fake engine returns `{stopped: True, final_state: "stopped"}` within 2 s. Pre-existing test continues to pass.
- Edge case (the regression): fake engine configured with `FAKE_FINALIZE_BEFORE_TERM=1` — emits `recording_finalized` and exits the moment `stop()` is called. Without the fix, `stop()` blocks for the full 30 s `stop_timeout`. With the fix, returns within 1 s with `final_state: "stopped"`. **Covers AE-A.**
- Edge case: `stop()` called when `_proc is None` (no engine ever spawned) takes the early-return path at line 280-282; no subscription created, no leak.
- Edge case: `stop()` called when `_proc.is_alive()` is false at the check (engine already dead via different path) takes the early-return path at line 306-316 unchanged; no leak.
- Error path: `_exit_poll` publishes `recording_finalized` AFTER `subscribe(since=...)` returns — replay buffer is empty for that event, but live delivery via the queue covers it normally. Verifiable by ordering control in the fake engine.
- Integration scenario: `recording.stop` HTTP call against a daemon whose engine self-exits in the await gap (wire-level analog of the unit test). Returns within 1 s with `final_state: "stopped"`.

**Verification:**
- The regression test fails deterministically before the fix and passes after.
- `pytest tests/daemon -x` clean.
- `Supervisor.shutdown()` analogous test (engine self-exits during shutdown) passes within 1 s.

---

### U4. SwiftUI mock-server timing fix in `RecorderControllerDaemonTests`

**Goal:** Replace the test that masks the production race (mock delivers `started` AFTER subscribe) with a test that mirrors production timing (the mock has the event ready BEFORE the subscribe arrives, and SwiftUI receives it via the daemon's replay path). Verifies R2 from the client side.

**Requirements:** R2, R7

**Dependencies:** U1, U2 (the daemon side must honor replay first; the SwiftUI client already passes `sinceCursor` correctly).

**Files:**
- Modify: `macos/ScreencapTests/RecorderControllerDaemonTests.swift`
- (Read for reference, no change expected: `macos/ScreencapTests/DaemonClientTests.swift` — `UnixHTTPTestServer` chunked-response handling.)

**Approach:**
- Keep the existing happy-path test as a positive control (live delivery — which still works).
- Add a new test `testProductionOrderingStartedBeforeSubscribe` that:
  1. Configures the embedded `UnixHTTPTestServer` so the `/v0/recording.start` response carries `cursor=N`, and the server records `recording_started` with `cursor=N+1` into a server-side ring before the SwiftUI controller issues `/v0/events?since=N`.
  2. When SwiftUI's subscribe request arrives, the test server emits `recording_started` (cursor `N+1`) BEFORE any live event, mirroring the daemon's replay behavior.
  3. Asserts `RecordingState` transitions through `.starting` → `.recording` within a deterministic timeout (e.g., 500 ms), with `recordingStartedAt` populated from the event.
- The test server gains a small "preset replay" hook — a method that lets the test queue events keyed by cursor, which the server emits to any `/v0/events?since=<cursor>` subscriber. Pure test fixture — does not need to mirror the daemon's eviction semantics.

**Test scenarios:**
- Happy path (preserved): existing `testStartTransitionsToRecording` passes unchanged. **Positive control.**
- Edge case (the regression): `testProductionOrderingStartedBeforeSubscribe` — mock server has `recording_started` queued at `cursor=N+1` BEFORE SwiftUI issues `/v0/events?since=N`; SwiftUI's `RecordingState` transitions to `.recording` within 500 ms. **Covers AE-B from the client side.**
- Edge case: mock server returns HTTP 410 `cursor_unknown` for `?since=N`; SwiftUI surfaces the existing `cursor_unknown` failure path (controller falls back to a fresh snapshot fetch). Verifiable via the existing `DaemonClientError` mapping.
- Error path: mock server delivers a `recording_failed` event in the replay window; SwiftUI surfaces the failure to the recorder UI.

**Verification:**
- New test fails before the daemon-side fixes (U1+U2) and the test server's preset-replay support — confirms it actually exercises the production race shape.
- After this unit lands, the new test passes deterministically; the existing positive-control test still passes.

---

### U5. `EX_TEMPFAIL=75` exit + lsof rogue-PID logging + `InstallResult.state` for already-running

**Goal:** When a same-EUID rogue process holds `~/.screencap/run/api.sock`, the daemon exits with code 75 and logs the offending PID; the CLI install verifier surfaces this as `install_failed_already_running`.

**Requirements:** R3, R4, R7

**Dependencies:** None.

**Files:**
- Modify: `src/screencap/daemon/socket.py` (lsof PID capture in `_probe_existing_socket`; `existing_pid` attribute on `DaemonAlreadyRunning`)
- Modify: `src/screencap/daemon/server.py` (return 75 on `DaemonAlreadyRunning`; log PID via `logger.warning`)
- Modify: `src/screencap/daemon/launchagent.py` (add `install_failed_already_running` state to the install verifier; check `last exit code = 75` from `launchctl print` to detect)
- Modify: `src/screencap/cli.py` (consume new state in human-readable serve install output)
- Test: `tests/test_serve_command.py` (exit-code-75 regression; `InstallResult.state` assertion)
- Test: `tests/daemon/test_socket.py` if exists, else add (lsof capture under the rogue-bind scenario)

**Approach:**
- Inside `_probe_existing_socket` in `socket.py:65-84`, after the `connect()` succeeds (which is what raises `DaemonAlreadyRunning`), call `subprocess.run(["lsof", "-tU", str(path)], timeout=1.0, capture_output=True, text=True)`. Parse stdout (`"<pid>\n"`) into an int, defaulting to `None` on any error/timeout. Pass the PID up by adding `existing_pid: int | None` to `DaemonAlreadyRunning.__init__`.
- In `server.py:124-127`, the `except DaemonAlreadyRunning` branch:
  - Log `logger.warning("daemon socket already bound by pid=%s", exc.existing_pid or "unknown")`.
  - User-facing `Console(stderr=True)` output gets a one-liner including the PID.
  - Return `75` instead of `1`. Leave the `except RogueFileAtSocketPath` branch returning `1` unchanged.
- In `launchagent.py`, the install verifier (whichever method confirms post-install daemon health — likely `_wait_for_daemon` or an analog) gets a new state arm: after `launchctl bootstrap` returns success, if the daemon never reaches "running" within the timeout AND `launchctl print gui/$UID/<label>` shows `last exit code = 75`, set `InstallResult.state = "install_failed_already_running"` with a `detail` field carrying the rogue PID if available.
- `cli.py`'s human-readable serve install output gets a new branch printing "Another daemon is already bound to {socket}; pid={pid}. Try `screencap serve --uninstall` or kill pid {pid}."

**Test scenarios:**
- Edge case (the regression for exit code): pre-bind the socket from a separate `subprocess.Popen` that holds an `AF_UNIX` listener at `~/.screencap/run/api.sock`. Run `screencap serve` in a child; observe exit code 75 (not 1). **Covers AE-C item 1.**
- Edge case: same scenario; assert the daemon's stderr contains a log line matching `r"daemon socket already bound by pid=\d+"`. **Covers AE-C item 2.**
- Edge case: `screencap serve --install` against a pre-bound socket. The install verifier returns `InstallResult.state == "install_failed_already_running"`. **Covers AE-C item 3.**
- Error path: `lsof` not on PATH (or fails for any reason) — the daemon still exits with code 75; the log line says `pid=unknown`.
- Error path: `lsof` blocks longer than 1 s — the timeout fires; the daemon still exits with code 75 within seconds of the bind failure.
- Edge case: a `RogueFileAtSocketPath` (a non-socket file at the path) still exits with code 1. Verify the two paths are not collapsed.

**Verification:**
- Exit code 75 reproducible via the regression test.
- `launchctl print gui/$UID/<label>` after a real-system rogue-bind scenario shows `last exit code = 75` (manual smoke runbook).
- `InstallResult.state == "install_failed_already_running"` reproducible in the install verifier test.

---

### U6. `F_SETPIPE_SZ` widening on engine stderr

**Goal:** Widen the engine's stderr kernel pipe to 1 MiB after `subprocess.Popen`, with empirical fallback to 128 KiB on `EINVAL`. Adds a regression test that demonstrates the engine continues to emit events through the bridge while the `_stderr_pump` is briefly paused.

**Requirements:** R5, R7

**Dependencies:** None. (Logically independent of U1–U5; can land in any order.)

**Files:**
- Modify: `src/screencap/daemon/supervisor.py` (call `fcntl.fcntl(stderr_fd, F_SETPIPE_SZ, 1 << 20)` after Popen, with try/except for `EINVAL` → fall back to `1 << 17`)
- Test: `tests/daemon/test_supervisor.py` (regression test — pause `_stderr_pump`, observe engine continues to emit > 16 KiB of events without blocking)

**Approach:**
- Add a `_widen_stderr_pipe(proc)` helper near the engine spawn in `supervisor.py`. Calls `fcntl.fcntl(proc.stderr.fileno(), F_SETPIPE_SZ, 1 << 20)`. On `OSError` with `errno.EINVAL`, retry with `1 << 17`. Log the size that was actually applied at INFO.
- `F_SETPIPE_SZ` constant comes from `fcntl` on Linux; on macOS, the value is the same numeric (`F_SETPIPE_SZ = 1031` is Linux-only; macOS uses a different mechanism). Verify at implementation time — macOS's `fcntl` may not expose `F_SETPIPE_SZ` at all. If it doesn't, the fallback is to use the `F_SETSIZE` family or accept that the widening is a no-op on macOS and rely on the regression test to surface that. **This is the planning-time unknown deferred to implementation.**
- If the widening is genuinely a no-op on macOS, the unit test must still pass (it asserts pump-stall behavior, not pipe size); document the limitation in a one-line comment beside the call.
- Call site: immediately after `subprocess.Popen(..., stderr=subprocess.PIPE)` in the engine spawn path, before `_stderr_pump` starts.

**Test scenarios:**
- Happy path: spawn fake engine; assert the chosen pipe size > the platform default (read back via `fcntl.fcntl(fd, F_GETPIPE_SZ)` on Linux; on macOS, the test asserts the helper did not raise and proceeds).
- Edge case (the regression): pause `_stderr_pump` (e.g., mock the `to_thread(stderr.readline)` call to await on a test event); fake engine emits 200 events of `~100 bytes` each (≈ 20 KiB). Without the widening, the engine blocks before all 200 land (default ~64 KiB / 100 = ~640 events, so this scenario doesn't trigger blocking on its own — increase to a size that DOES trigger blocking with the default, e.g., 1000 events for ~100 KiB). With the widening, all 1000 land in the kernel pipe before pump resumes.
- Edge case: `_stderr_pump` resumes; all 1000 events flow through the bus and reach a fast subscriber in order. Cursor stamps are monotonic.
- Error path: `fcntl` raises `EINVAL` for `1 << 20`; helper falls back to `1 << 17` and logs the fallback. Verifiable by mocking `fcntl.fcntl` to raise once.
- Error path: `fcntl` raises any other error; helper logs a warning and continues (engine still spawns, no crash). The pipe stays at the default size.

**Execution note:** The TKT-D ticket's "EventBus.publish awaits put per subscriber" claim is incorrect against current code — `publish()` uses `put_nowait` and closes overflowed subscribers. The fix in this unit does NOT depend on that claim. Add a one-line comment at the F_SETPIPE_SZ call site noting "buys headroom for `_stderr_pump` against engine-side burst writes" — not "decouples bridge from bus".

**Verification:**
- Engine emits a burst of events while `_stderr_pump` is artificially paused; events arrive at a fast subscriber in order after the pump resumes.
- `pytest tests/daemon -x` clean.
- The chosen pipe size is documented in a code comment with the `1 << 20` value and the fallback rationale.

---

## System-Wide Impact

- **Interaction graph:** EventBus is the central fan-out for daemon lifecycle events; replay support (U1) touches every consumer that uses `?since=` (today: SwiftUI's `RecorderController`, the `mcp_contract_smoke.py` smoke). The TOCTOU fix (U3) is internal to `Supervisor`; the rogue-bind diagnostics (U5) only fire on startup; F_SETPIPE_SZ (U6) only applies on engine spawn.
- **Error propagation:** The `cursor_unknown` 410 path was already wired but unreachable for the `since < current` case; U2 makes it reachable, which is a behavior tightening for any client that was relying on the silent "live from now" fallback. Audit: only `mcp_contract_smoke.py` and the test suite use `?since=`; both pass valid cursors.
- **State lifecycle risks:** Replay buffer survives across recording sessions (it's bus-scoped, not session-scoped). Worst case: a stale `recording_finalized` event from an earlier session shows up in a fresh subscriber's replay. Mitigation: subscribers always pair `since=` with a fresh snapshot fetch (RecorderController already does); the cursor is monotonic so deduplication is trivial. Document this in the code comment beside the buffer.
- **API surface parity:** `?since=` semantics are now consistent across the three valid ranges (future cursor → 410, retained range → replay, too-old → 410). The HTTP envelope shape is unchanged; only the set of cursor values that produce 410 grows. Per-route schema version not bumped (additive behavior).
- **Integration coverage:** U2 (wire-level replay test) and U4 (SwiftUI test server flip) together prove that `recording.start` → `events?since=cursor` → `started` works end-to-end across the daemon and client. This is the first time that contract is exercised in CI; previously the SwiftUI test masked it.
- **Unchanged invariants:** The `EventBus.publish()` non-blocking semantics (uses `put_nowait`, closes on `QueueFull`) are explicitly preserved. The per-subscriber `QUEUE_MAXSIZE = 1024` live cap is unchanged. The `slow_consumer` close reason is unchanged. The 30 s `stop_timeout` is unchanged. The Phase 1 plan's "no replay buffer" promise is intentionally broken in favor of two real consumers; the deferred-section bullet is being retired.

---

## Risks & Dependencies

| Risk | Mitigation |
|------|------------|
| Replay buffer adds memory pressure on long-running daemons. | Bounded at 256 events × O(1 KB)/event = ~256 KB worst case. Fixed cost, asserted in U1's verification. |
| Replay events delivered out of order under concurrent publish + subscribe. | `subscribe()` enqueues replay events under the bus lock before adding the new subscription to `_subscribers`; live publish thereafter sees the new sub and delivers in cursor order. Test scenario in U1 covers concurrent shape. |
| `F_SETPIPE_SZ` is Linux-specific; macOS may not honor it. | U6 documents this as a deferred-to-implementation question. If macOS rejects, the fallback is a no-op widening — the regression test still passes (it asserts engine-survives-paused-pump behavior, not pipe size). Worst case is "the kernel pipe ceiling stays at the macOS default ~64 KB, and the slow-pump scenario can still block the engine"; in that case TKT-D approach (2) becomes the next step (deferred). |
| `lsof` is invoked on a hot startup path. | Best-effort with 1 s timeout; failure logs `pid=unknown` and continues. Never blocks daemon startup. |
| TKT-D ticket's incorrect rationale may cause future readers to design against the wrong mechanism. | Plan calls out the discrepancy in Key Technical Decisions and U6's Execution note. Deferred follow-up: amend the ticket body. |
| TKT-A regression test relies on `FAKE_FINALIZE_BEFORE_TERM=1` env-var hook in the fake engine script. | Pattern already exists for other env vars in `tests/daemon/test_supervisor.py:36-103`; consistent with the established fixture style. |
| `/v0/events?since=N` 410 for `N < current − retained_window` is a behavior tightening. | Only consumers using `?since=` are SwiftUI (passes valid recent cursors) and the smoke (same). No external consumers documented. The Phase 1 plan promised this behavior; this is bug-fix-aligned-with-plan. |

---

## Documentation / Operational Notes

- After this lands, retire the "Ring-buffer event replay on subscribe" bullet under "Deferred to Follow-Up Work" in [docs/plans/2026-05-08-001-feat-daemon-architecture-phase-1-plan.md](docs/plans/2026-05-08-001-feat-daemon-architecture-phase-1-plan.md). It's now done.
- Capture-worthy after merge (per `docs/solutions/` conventions): one solution doc covering the "late-listener race" pattern, citing the SIGINT-handler-timing precedent and the EventBus replay fix as the latest iteration. Strong signal that this shape recurs in the codebase.
- TKT-C operator UX: a real-system rogue-bind scenario should let the operator run `launchctl print gui/$UID/com.screencap.daemon` and see `last exit code = 75`. Document this in the install verifier's user-facing message.
- TKT-D measurement note: if production traces eventually show `_stderr_pump` stalls > 500 ms, that's the trigger to implement TKT-D approach (2) — file a follow-up ticket at that point.

---

## Sources & References

- TKT-A: [docs/tickets/2026-05-08-fix-stop-toctou-subscribe-before-check.md](docs/tickets/2026-05-08-fix-stop-toctou-subscribe-before-check.md)
- TKT-B: [docs/tickets/2026-05-08-fix-recorder-state-machine-missed-started-event.md](docs/tickets/2026-05-08-fix-recorder-state-machine-missed-started-event.md)
- TKT-C: [docs/tickets/2026-05-08-fix-rogue-bind-exit-code-distinction.md](docs/tickets/2026-05-08-fix-rogue-bind-exit-code-distinction.md)
- TKT-D: [docs/tickets/2026-05-09-fix-stderr-bridge-backpressure.md](docs/tickets/2026-05-09-fix-stderr-bridge-backpressure.md)
- Phase 1 origin plan: [docs/plans/2026-05-08-001-feat-daemon-architecture-phase-1-plan.md](docs/plans/2026-05-08-001-feat-daemon-architecture-phase-1-plan.md)
- Origin brainstorm: [docs/brainstorms/2026-05-08-cli-gui-mcp-architecture-requirements.md](docs/brainstorms/2026-05-08-cli-gui-mcp-architecture-requirements.md)
- Late-listener race precedent (SIGINT, 4 historical iterations): [docs/solutions/runtime-errors/sigint-handler-timing-and-recording-stop-methods.md](docs/solutions/runtime-errors/sigint-handler-timing-and-recording-stop-methods.md)
- Kernel-pipe pitfall (Foundation.Process side): [docs/solutions/integration-issues/macos-foundation-process-pipe-pitfalls.md](docs/solutions/integration-issues/macos-foundation-process-pipe-pitfalls.md)
- launchctl exit-code precedent: [docs/solutions/runtime-errors/launchd-plist-tilde-expansion-2026-05-09.md](docs/solutions/runtime-errors/launchd-plist-tilde-expansion-2026-05-09.md)
- Closed-set principle (replay design): [docs/solutions/runtime-errors/chunk-upload-sentinel-gating-and-data-loss.md](docs/solutions/runtime-errors/chunk-upload-sentinel-gating-and-data-loss.md)
