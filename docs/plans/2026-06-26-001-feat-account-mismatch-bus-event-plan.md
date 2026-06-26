---
title: "feat: Emit /v0/events account_mismatch bus event (SCR-171)"
type: feat
status: completed
date: 2026-06-26
deepened: 2026-06-26
---

# feat: Emit /v0/events account_mismatch bus event (SCR-171)

## Summary

Lift the already-detected cloud account-ownership mismatch onto the daemon's `/v0/events` SSE bus as a new advisory `account_mismatch` event, so a subscribed agent or the macOS app learns of a blocked-upload condition by push instead of polling `recording.list` + `whoami`. The detection signal already exists (the terminal-stage account gate); the work is to (1) make that signal structured rather than a free-text `upload_warning`, (2) publish it once from the single daemon resume path that observes it, and (3) document and wire-test the new event on the existing bus contract — with no schema-version bump and no new polling infrastructure.

**Scope of the push, stated honestly:** this increment delivers push for mismatch **detection** only. Reacting to *resolution* — the user re-signing-in as the owner so a previously-blocked upload can proceed, which is the motivating use case below — still requires polling `whoami` until the deferred `account_resolved` event lands (see Deferred to Follow-Up Work). The follow-up is therefore *required* for the full motivating loop, not optional polish.

---

## Problem Frame

SCR-148 (PR #264) delivered the **pull** half of cloud account-mismatch visibility: `owner_uid` / `upload_warning` on `recording.list`, the `/v0/auth.whoami` verb, and the MCP `whoami` tool. It deliberately deferred the **push** half. Today an agent that wants to react to an account change — e.g. to notice the user re-signed-in so a previously-blocked upload can proceed — must poll `list_recordings` + `whoami`, which adds latency to reactive workflows. The daemon already *computes* the mismatch at startup-sweep / resume time and logs a `WARNING`; that signal simply never reaches the event bus. This plan closes that gap for the **mismatch-detection** direction — surfacing the *blocked* condition by push. Detecting the *resolution* transition (so a blocked upload can auto-proceed once the right account signs back in) is deferred to a follow-up `account_resolved` event; until then an agent that receives `account_mismatch` still polls `whoami` to learn when the block clears. (Origin: [SCR-171](https://linear.app/zk-email/issue/SCR-171), follow-up to [SCR-148](https://linear.app/zk-email/issue/SCR-148).)

---

## Requirements

- R1. Emit a new `account_mismatch` event on the existing `/v0/events` SSE bus when the daemon detects, during terminal-stage resume, that a cloud recording's pinned `owner_uid` differs from the currently signed-in uid.
- R2. The event payload carries the recording's `owner_uid` and directory name plus the current signed-in identity sourced from `whoami`, emitted under **disambiguated** field names `signed_in_uid` / `signed_in_email` / `stale` (so the signed-in uid is never confused with `owner_uid`). `whoami` omits `stale` entirely on success, so the payload normalizes it to `false`; `signed_in_email`/`stale` may be null/`true` when auth is stale, and `signed_in_uid` (gate-sourced) is the authoritative comparison field.
- R3. The event is **advisory** — no consumer is required to treat it as terminal, and it changes no existing convergence/refusal behavior. The gate still refuses cloud convergence and keeps the recording local.
- R4. The event stays inside the same-EUID trust boundary (it rides the existing `/v0/events` socket, which already exposes `whoami` email/uid to same-EUID callers per `SECURITY.md`).
- R5. Adding the event must not break existing `/v0/events` consumers (tolerant-reader contract) and must not require an `EVENT_SCHEMA_VERSION` or `_EVENTS_API_VERSION` bump.
- R6. The new event type is documented in the canonical stderr/event schema doc.

---

## Scope Boundaries

- Not changing the account-mismatch **gate** itself — it continues to refuse cloud convergence and keep the recording local (`terminal_stage.py` `_route_cloud`).
- Not adding a mid-session active account-switch **watcher** (polling the keychain/token) — that contradicts the "push, not poll" goal and is out of scope.
- Not emitting from the **in-engine-subprocess** terminal-stage finalize path — it runs in the engine process (no daemon bus) and is re-mint-guarded against the live account. **NOTE (scope correction):** this is distinct from the daemon's **post-engine-exit resume**, which `_handle_engine_exit` funnels through `resume_terminal_stage` (the emit point, [`src/screencap/daemon/supervisor.py`](src/screencap/daemon/supervisor.py) line 905) and which reads the *Keychain* (the current account), not the engine token. So if the user switches accounts between stopping a recording and that detached resume firing, the gate trips and the event legitimately emits there too — that path is **in scope** and covered by the single emit point (see U2).
- Not emitting from the CLI `screencap upload` path — it runs outside the daemon and has no event bus; it already prints the `upload_warning` to the console.
- Not bumping any schema/API version.

### Deferred to Follow-Up Work

- **MCP server push surfacing** of `account_mismatch` to the agent: the FastMCP stdio transport is request/response with no server→client push channel; the MCP `LivenessSubscription._drain` stays drain-only. A future MCP notification/tool surface is a separate follow-up.
- **macOS app reaction**: wiring `RecorderController.swift` (or equivalent) to consume the new event and surface a re-login prompt is a separate `macos/` change.
- **A paired `account_resolved` / recovery event** (the positive mismatch→match transition that signals "re-login succeeded, retry the upload"). Detecting resolution needs either a watcher or a resume-success hook; deferred.

---

## Context & Research

### Relevant Code and Patterns

- **Event bus + emit helper** — [`src/screencap/daemon/event_bus.py`](src/screencap/daemon/event_bus.py) (`EventBus.publish`, 256-event replay `deque`, monotonic cursor, `subscribe(since=cursor)` under the same lock as publish). Daemon emit helper `Supervisor._publish_daemon_event` at [`src/screencap/daemon/supervisor.py`](src/screencap/daemon/supervisor.py) (~line 1090) builds `{type, schema_version, ts, **fields}`, calls `_observe_event` (idle-shutdown liveness), then `_bus.publish`.
- **Event-type constants** — [`src/screencap/_stderr_events.py`](src/screencap/_stderr_events.py): `EVENT_SCHEMA_VERSION = 1` (line 64); the "Daemon bus events" block (lines 103-109) is where `engine_crashed`, `previous_session_recovered`, etc. live and where the new constant belongs. Existing advisory precedent: `EVENT_CAPTURE_UNHEALTHY` / `EVENT_CAPTURE_RECOVERED` (lines 85-96).
- **The detection signal (already computed)** — [`src/screencap/terminal_stage.py`](src/screencap/terminal_stage.py) `_route_cloud` account gate (lines 896-913): reads `read_owner_uid(recording_dir)`, computes `current_uid = auth.id_token_uid(auth.get_id_token())`, and on `current_uid != owner_uid` sets `result.upload_warning` and returns. `TerminalResult` dataclass at lines ~138-164.
- **The single daemon observation point** — `Supervisor.resume_terminal_stage` ([`src/screencap/daemon/supervisor.py`](src/screencap/daemon/supervisor.py) line 495) runs `run_terminal_stage(..., non_blocking=True)` in a worker thread and is the common path called by both `_run_startup_sweep` (line 635) and `_run_resume_safely` (line 579, the crash/restart resume).
- **Payload field sources** — `auth.whoami()` ([`src/screencap/auth.py`](src/screencap/auth.py) line 714) returns `signed_in`/`uid`/`email`/`stale`; `owner_uid` via `catalog.read_owner_uid` ([`src/screencap/catalog.py`](src/screencap/catalog.py) lines 36-47).
- **`/v0/events` wire endpoint** — `events_stream` ([`src/screencap/daemon/app.py`](src/screencap/daemon/app.py) lines 585-689), NDJSON with cursor stamping; route registered ~line 1014. `_EVENTS_API_VERSION = 1` in [`src/screencap/daemon/schema.py`](src/screencap/daemon/schema.py).
- **Schema doc** — [`docs/research/2026-04-28-stderr-event-schema.md`](docs/research/2026-04-28-stderr-event-schema.md) "### Daemon bus events" (line 118) enumerates the daemon bus event types; new event must be appended there.

### Institutional Learnings

- [`docs/solutions/runtime-errors/eventbus-late-listener-replay-2026-05-12.md`](docs/solutions/runtime-errors/eventbus-late-listener-replay-2026-05-12.md) — the bus retains the last `REPLAY_BUFFER_SIZE = 256` events; `subscribe(since)` replays `cursor > since` under the publish lock, so a publish in the await gap is never lost. **Testing trap:** tests that deliver events *after* the subscriber joins mask the production race; the new event's wire test must publish *before* the subscriber joins and assert replay delivery via `since=cursor`. Out-of-range cursor → `CursorOutOfRangeError` → HTTP 410 (a future forwarder must refetch a snapshot, not collapse to "live from now").
- [`docs/solutions/runtime-errors/capture-health-nonscreen-attribution-terminal-stop-2026-06-01.md`](docs/solutions/runtime-errors/capture-health-nonscreen-attribution-terminal-stop-2026-06-01.md) — an event's advisory-vs-terminal contract is enforced by what the consumer does with it (`permission_lost` → `stop()` tore down a healthy recording). Give `account_mismatch` its own constant; no consumer may map it to teardown. The recurring miss was tests blessing emission in isolation while the cross-boundary invariant went untested.
- [`docs/solutions/integration-issues/daemon-start-failure-typed-error-before-spawn-2026-06-08.md`](docs/solutions/integration-issues/daemon-start-failure-typed-error-before-spawn-2026-06-08.md) — async events are the *wrong* channel for a fatal precondition on an in-flight request, but the *right* channel for an asynchronously-detected advisory condition with no single originating request (exactly this case). Also: the long-lived daemon caches launch-time auth state; use `auth.whoami` semantics for "who is signed in *now*".

### External References

- None required. The work follows established in-repo patterns (daemon bus emit, advisory-event precedent, tolerant-reader schema). No external/library research warranted.

---

## Key Technical Decisions

- **Structured discriminator, not string-matching.** `upload_warning` is overloaded — set by the account gate *and* by scrub/mask failures and upload failures ([`src/screencap/terminal_stage.py`](src/screencap/terminal_stage.py) lines 909, 983, 992, 1014, 1031, 1052). Driving the event off "`upload_warning` is truthy" would false-emit for non-account failures. Add a dedicated structured field (e.g. an `AccountMismatch` value carrying `owner_uid` + `signed_in_uid`) on `TerminalResult`, populated only by the gate. Keep `upload_warning` set as-is for back-compat (CLI/shutdown output still reads it).
- **Single emit point = `resume_terminal_stage`.** All daemon detection paths funnel through it: `_run_startup_sweep` (line 635), `_run_resume_safely` on crash/restart resume (line 579), and `_run_resume_safely` reached via `_handle_engine_exit` for post-engine-exit resume (line 905). Emitting there covers all three with no double-fire and no change to the sweep's existing logging. (Only the in-engine-subprocess finalize path — a separate process — is excluded; see Scope Boundaries.) Rationale also covers the user-confirmed trigger decision (resume-path detection).
- **No `EVENT_SCHEMA_VERSION` / `_EVENTS_API_VERSION` bump.** The version covers the envelope shape (`{type, schema_version, ts, ...}`); a new `type` value is purely additive and the tolerant-reader contract means existing consumers ignore unknown types.
- **Payload shape:** `{recording: <dir name>, owner_uid, signed_in_uid, signed_in_email, stale}`. `owner_uid` and `signed_in_uid` come from the gate (authoritative for the comparison that fired the event). `signed_in_email` + `stale` are enriched via `await asyncio.to_thread(auth.whoami)` at emit time — **off-loop**, mirroring the `/v0/auth.whoami` handler at [`src/screencap/daemon/app.py`](src/screencap/daemon/app.py) line 209, because `whoami` does blocking Keychain + token-refresh I/O (a refresh can take up to its 30 s timeout) and must never run on the daemon event loop. `whoami` returns `{signed_in, uid, email}` on success with **no** `stale` key, so the emit normalizes `stale` via `.get("stale", False)`; on a stale/offline auth it returns `uid=None, email=None, stale=True`, so `signed_in_email`/`stale` may be null/`true` in the payload while `signed_in_uid` (from the gate) stays authoritative. `signed_in_uid` is never null — the gate only fires when `current_uid` is non-null and differs from `owner_uid`.
- **Advisory, never terminal.** Own constant under the "Daemon bus events" block; documented as advisory in the schema doc; no consumer maps it to stop/teardown.
- **Stateless re-emission (no cross-run dedup/coalescing).** A mismatch re-emits each time a resume re-detects it; advisory re-confirmation is desirable, and late subscribers are covered by the existing 256-event replay buffer + cursor. Time-based eviction and coalescing were explicitly rejected as bus conventions (replay learning). Re-emission count scales with (mismatched recordings × resume passes), and the 256-entry replay ring is shared across all event types — for an unusual high-mismatch-count or crash-looping deployment this could pressure other event types out of a late subscriber's replay window. Acceptable for the expected case; noted so a future high-volume deployment isn't surprised (revisit dedup only if that materializes).
- **Fail-open emit.** A publish failure must never break resume convergence; the emit is wrapped so an exception is logged and swallowed, consistent with `resume_terminal_stage`'s existing fail-closed/open discipline.

---

## Open Questions

### Resolved During Planning

- **Trigger** (the ticket's open decision): resume-path detection — emit from `resume_terminal_stage`, covering startup sweep + crash/restart resume; no mid-session watcher. (User-confirmed.)
- **Schema bump?** No — additive new type under the tolerant-reader contract.
- **How to distinguish from other `upload_warning` causes?** Structured `AccountMismatch` field populated only by the gate.
- **Dedup semantics?** Stateless; rely on replay buffer for late subscribers.

### Deferred to Implementation

- Exact name/shape of the structured field and any helper (`AccountMismatch` dataclass vs. a small typed dict) — pick whatever fits `TerminalResult`'s existing dataclass style.
- Whether to enrich `signed_in_email`/`stale` via `await asyncio.to_thread(auth.whoami)` at emit time or to carry them from the gate. Default is the off-loop `whoami` call; the implementer may simplify to gate-carried fields, which is materially simpler (it avoids the second auth read, the `to_thread` hop, and the `stale`-key normalization). Either way the enrichment must not block the event loop and `signed_in_uid` stays gate-sourced and authoritative.
- Exact test fixtures for a bus subscriber (direct `EventBus.subscribe` vs. the HTTP stream) per unit — see per-unit test scenarios.

---

## Implementation Units

### U1. Structured account-mismatch signal on `TerminalResult`

**Goal:** Make the account-mismatch detection a structured, machine-readable field so a specific event can be driven off it without parsing `upload_warning`.

**Requirements:** R1, R2 (partial — provides `owner_uid` + `signed_in_uid`)

**Dependencies:** None

**Files:**
- Modify: `src/screencap/terminal_stage.py` (add the structured `AccountMismatch` type + field on `TerminalResult`; populate it in the `_route_cloud` gate at lines 896-913 alongside the existing `upload_warning`)
- Test: `tests/test_terminal_stage.py`

**Approach:**
- Add a small structured value (`AccountMismatch` frozen dataclass with `owner_uid: str`, `signed_in_uid: str`, or an equivalent typed dict) and a `account_mismatch: AccountMismatch | None = None` field on `TerminalResult`.
- In the gate, when `current_uid is not None and current_uid != owner_uid`, set both the new structured field and the existing `upload_warning` (do not remove the string — CLI and shutdown messaging still consume it), then return as today.
- No behavior change to refusal/convergence — purely additive data on the result.

**Patterns to follow:**
- Existing `TerminalResult` dataclass fields ([`src/screencap/terminal_stage.py`](src/screencap/terminal_stage.py) lines 138-164) — informational, default-valued fields.

**Test scenarios:**
- Happy path: owner pinned to `uid-A`, signed in as `uid-B` → `result.account_mismatch` is populated with `owner_uid="uid-A"`, `signed_in_uid="uid-B"`, AND `result.upload_warning` still contains "account mismatch" (extend `test_account_mismatch_refuses_cloud_convergence`).
- Edge case: matching account (`uid-A` == `uid-A`) → `result.account_mismatch is None` (extend `test_matching_account_proceeds_to_upload`).
- Edge case: legacy/no owner pin → `result.account_mismatch is None` (extend `test_no_owner_pin_legacy_recording_not_refused`).
- Edge case: not signed in / undeterminable `current_uid` → gate does not refuse and `result.account_mismatch is None`.
- Discriminator guard: a run whose `upload_warning` is set by a scrub/mask or upload failure (not the gate) leaves `result.account_mismatch is None` — this is the key guard against the overloaded-field false positive.

**Verification:**
- `TerminalResult` carries an unambiguous account-mismatch signal that is set only by the gate and never by other `upload_warning` setters; existing terminal-stage tests still pass.

---

### U2. Emit `account_mismatch` from `resume_terminal_stage`

**Goal:** Publish the new advisory event on the daemon bus whenever the resume path observes a structured account mismatch, with a `whoami`-aligned payload.

**Requirements:** R1, R2, R3, R4, R5

**Dependencies:** U1

**Files:**
- Modify: `src/screencap/_stderr_events.py` (add `EVENT_ACCOUNT_MISMATCH = "account_mismatch"` under the "Daemon bus events" block, lines ~103-109, with an advisory doc comment)
- Modify: `src/screencap/daemon/supervisor.py` (`resume_terminal_stage`, line 495 — after the worker-thread result returns, if it carries `account_mismatch`, build the payload and publish via `_publish_daemon_event`; fail-open)
- Test: `tests/daemon/test_supervisor.py`

**Approach:**
- `resume_terminal_stage` today simply returns its worker-thread result with no post-result code — it gains new logic that runs **after** the `to_thread` await (on the loop, not inside the worker thread, which cannot `await` a publish): if the result carries `account_mismatch`, enrich with `await asyncio.to_thread(auth.whoami)` (for `signed_in_email` + `stale`, normalizing the absent `stale` key to `False`) and call `await self._publish_daemon_event(EVENT_ACCOUNT_MISMATCH, recording=<dir name>, owner_uid=..., signed_in_uid=..., signed_in_email=..., stale=...)`.
- The emit must live **inside** `resume_terminal_stage`, not in its callers: `_run_resume_safely` discards the result entirely, so a caller-side emit would miss the crash/restart and post-engine-exit paths.
- Use the gate-captured `owner_uid` / `signed_in_uid` as authoritative for the comparison; treat `whoami`-sourced `email`/`stale` as best-effort enrichment read a beat later.
- Wrap the emit so any failure (whoami error, publish error) is logged and swallowed — it must never break convergence. The publish itself rides `_publish_daemon_event` (which also feeds `_observe_event` idle-shutdown liveness — correct: an emitted event counts as activity).
- A `None` result (busy/fail-closed) or a result without `account_mismatch` publishes nothing.

**Execution note:** Start with a failing test asserting the event is published for a mismatch result and *not* published for a clean result.

**Patterns to follow:**
- `Supervisor._publish_daemon_event` and its existing call site for `EVENT_ENGINE_CRASHED` ([`src/screencap/daemon/supervisor.py`](src/screencap/daemon/supervisor.py) ~line 867).
- Advisory-event precedent `EVENT_CAPTURE_UNHEALTHY` / `EVENT_CAPTURE_RECOVERED` in [`src/screencap/_stderr_events.py`](src/screencap/_stderr_events.py).

**Test scenarios:**
- Happy path: `resume_terminal_stage` over a recording pinned to `uid-A` while signed in as `uid-B` (mock `whoami` → `{"signed_in": True, "uid": "uid-B", "email": "b@x"}` — note **no** `stale` key, matching the real success shape) publishes exactly one `EVENT_ACCOUNT_MISMATCH` with payload `{recording, owner_uid: uid-A, signed_in_uid: uid-B, signed_in_email: b@x, stale: False}` (the absent `stale` normalized to `False`) and envelope `schema_version == 1`. Assert by subscribing to the supervisor's `EventBus` directly.
- Edge case (enrichment auth stale): the gate produced the mismatch (`signed_in_uid = uid-B`), but the beat-later enrichment `whoami` returns `{"signed_in": True, "uid": None, "email": None, "stale": True}` → payload carries `signed_in_uid: uid-B` (gate, authoritative), `signed_in_email: null`, `stale: True`, and the event still emits.
- Edge case: a clean convergence result (no `account_mismatch`) publishes **no** `account_mismatch` event.
- Edge case: `resume_terminal_stage` returns `None` (busy / auth-unavailable / fail-closed) → no event, no exception.
- Discriminator: a result whose only `upload_warning` is a scrub/upload failure (no structured `account_mismatch`) publishes **no** `account_mismatch` event.
- Fail-open: a `whoami` or `publish` exception during emit is swallowed (logged) and `resume_terminal_stage` still returns its result; no exception escapes into the loop.
- Integration (startup sweep): `_run_startup_sweep` over one mismatched recording emits exactly one `account_mismatch` event (covers the ticket's primary path) and the existing `refused` WARNING log still fires.
- Integration (post-engine-exit): a resume reached via `_handle_engine_exit` over a recording whose `owner_uid` differs from the now-signed-in account emits exactly one `account_mismatch` — covers the corrected scope (a post-stop account switch is a real emit path, not an impossible one).
- Off-loop: assert the enrichment uses `asyncio.to_thread(auth.whoami)` (e.g. a `whoami` mock that would block does not stall the loop) — guards against re-introducing a loop-blocking call.
- Constant: `EVENT_ACCOUNT_MISMATCH == "account_mismatch"`.

**Verification:**
- A daemon startup sweep or crash/restart resume that detects a mismatch publishes exactly one advisory `account_mismatch` event with a `whoami`-aligned payload; no other path emits it; convergence/refusal behavior is unchanged.

---

### U3. Wire-level replay test + schema documentation

**Goal:** Prove the event is observable end-to-end over the `/v0/events` HTTP stream under the replay/cursor contract, and document it as a first-class daemon bus event.

**Requirements:** R5, R6

**Dependencies:** U2

**Files:**
- Modify: `docs/research/2026-04-28-stderr-event-schema.md` (append `account_mismatch` under "### Daemon bus events" with its payload fields and advisory semantics)
- Test: `tests/daemon/test_event_stream.py`

**Approach:**
- Add an integration test that publishes an `account_mismatch` event onto the bus **before** a subscriber joins, then subscribes with `since=<snapshot cursor captured before publish>` and asserts the frame is delivered via replay with a stamped `cursor` — mirroring the production timing the replay learning warns about (publish-in-the-await-gap), not the inverted timing that masks it.
- Doc update is the canonical contract record; it lists the new type alongside `engine_crashed` et al. and states it is advisory and same-EUID-bounded.

**Patterns to follow:**
- Existing `tests/daemon/test_event_stream.py` publish/read pattern (`test_events_stream_returns_ndjson_subscribed_frame_and_published_events`) and the `since=cursor` replay tests in `tests/daemon/test_event_bus.py`.
- The "Daemon bus events" doc section format in [`docs/research/2026-04-28-stderr-event-schema.md`](docs/research/2026-04-28-stderr-event-schema.md).

**Test scenarios:**
- Integration / replay timing: publish `account_mismatch` (cursor N), then `subscribe(since=N-1)` → the frame is replayed with `cursor == N` and the full payload intact. (Covers the late-subscriber race the replay learning flags.)
- Wire shape: the streamed NDJSON frame is valid JSON carrying `type == "account_mismatch"`, `schema_version`, `ts`, `cursor`, and the payload fields.
- Doc: `Test expectation: none` for the schema-doc edit — it is documentation; correctness is enforced by the wire test above.

**Verification:**
- A `/v0/events` subscriber reliably receives the `account_mismatch` frame even when it connects just after the publish; the schema doc lists the event with its advisory semantics and payload.

---

## System-Wide Impact

- **Interaction graph:** `resume_terminal_stage` is called by `_run_startup_sweep` (line 635) and `_run_resume_safely` (line 579), the latter reached both on crash/restart and via `_handle_engine_exit` (post-engine-exit, line 905). Emitting inside `resume_terminal_stage` covers all of these with no other callers and no double-fire. Only the **in-engine-subprocess** terminal-stage finalize path (a separate process, re-mint-guarded) is not an emit site.
- **Error propagation:** the emit is fail-open — a `whoami`/publish failure is logged and swallowed so resume convergence is never broken.
- **State lifecycle risks:** none — the event is stateless; nothing is persisted; re-emission across daemon restarts is intentional.
- **API surface parity:** `/v0/events` contract is unchanged except for the additive type; no new endpoint, no `_EVENTS_API_VERSION` / `EVENT_SCHEMA_VERSION` bump. Schema doc updated to match.
- **Integration coverage:** the producer-before-subscriber replay test (U3) is the cross-layer scenario unit tests alone won't prove.
- **Unchanged invariants:** the account-mismatch **gate** still refuses cloud convergence and keeps the recording local; `upload_warning` is still set; CLI/shutdown output is unchanged; `whoami` / `owner_uid` semantics are unchanged. This plan only **adds** an advisory event.

---

## Risks & Dependencies

| Risk | Mitigation |
|------|------------|
| False-positive emission for non-account `upload_warning` causes (scrub/upload failures) | Structured `AccountMismatch` discriminator set only by the gate; explicit discriminator test in U1 and U2 |
| Double-emission of the same mismatch | Single emit point in `resume_terminal_stage`; U2 test asserts exactly one event for a sweep over one mismatched recording |
| Late subscriber misses an event published during startup sweep | Existing 256-event replay buffer + `subscribe(since=cursor)`; U3 test exercises the publish-before-subscribe timing |
| Emit failure breaks resume convergence | Fail-open: emit wrapped in try/except, logged and swallowed; U2 fail-open test |
| `whoami` enrichment read inconsistent with gate uids (account changed between gate and emit) | Gate-captured `owner_uid`/`signed_in_uid` are authoritative for the comparison; `email`/`stale` are advisory enrichment; documented |
| `whoami` enrichment blocks the daemon event loop (it does blocking Keychain + ≤30 s token-refresh I/O) | Run it off-loop via `await asyncio.to_thread(auth.whoami)`, mirroring `daemon/app.py`'s `/v0/auth.whoami`; whole emit stays inside the fail-open guard; off-loop test in U2 |
| Payload asserts a `stale` key `whoami` doesn't return on success (KeyError / never-matching assertion) | Normalize via `.get("stale", False)`; U2 happy-path mock uses the real no-`stale` success shape |
| Privacy: email/uid on the bus | The `/v0/events` socket is already same-EUID-bounded and `whoami` already exposes email/uid to the same caller class (`SECURITY.md`); no new exposure |

---

## Documentation / Operational Notes

- Update [`docs/research/2026-04-28-stderr-event-schema.md`](docs/research/2026-04-28-stderr-event-schema.md) "### Daemon bus events" with `account_mismatch` (payload + advisory semantics) — part of U3.
- Worth a `/ce-compound` capture afterward: the MCP-forwarding / 410-reconnect interplay and the SCR-148→SCR-171 push/pull split currently have no `docs/solutions/` entry.

---

## Sources & References

- **Origin ticket:** [SCR-171 — Emit /v0/events account_mismatch bus event](https://linear.app/zk-email/issue/SCR-171)
- **Parent:** [SCR-148 — Surface cloud account-mismatch on daemon API / MCP](https://linear.app/zk-email/issue/SCR-148) (PR [#264](https://github.com/proteus-computer-use/screencap/pull/264))
- Related code: `src/screencap/daemon/event_bus.py`, `src/screencap/daemon/supervisor.py`, `src/screencap/terminal_stage.py`, `src/screencap/_stderr_events.py`, `src/screencap/auth.py`, `src/screencap/catalog.py`
- Schema contract: `docs/research/2026-04-28-stderr-event-schema.md`
- Learnings: `docs/solutions/runtime-errors/eventbus-late-listener-replay-2026-05-12.md`, `docs/solutions/runtime-errors/capture-health-nonscreen-attribution-terminal-stop-2026-06-01.md`, `docs/solutions/integration-issues/daemon-start-failure-typed-error-before-spawn-2026-06-08.md`
- Branch: `rutefig/scr-171-emit-v0events-account_mismatch-bus-event-scr-148-push-signal`
