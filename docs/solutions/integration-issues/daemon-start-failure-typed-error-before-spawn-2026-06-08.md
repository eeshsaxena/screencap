---
title: "Daemon start-failures must be raised before EVENT_STARTED — a worker that signals 'started' then SystemExits turns a fatal precondition into an unobservable async crash"
date: 2026-06-08
category: integration-issues
module: daemon
problem_type: integration_issue
component: daemon
symptoms:
  - "recording.start returns 200 OK, then the recording immediately collapses via async permission_lost / engine_crashed / recording_finalized events"
  - "The app cannot tell at the call site that the start failed for a missing permission — it gets a success, then a crash"
  - "Daemon-path start only ever preflighted Screen Recording; Accessibility / Input Monitoring were never start-gated"
root_cause: design_flaw
resolution_type: code_fix
severity: medium
related_components:
  - daemon
  - macos-app-shell
tags:
  - macos
  - daemon
  - ipc
  - tcc
  - permissions
  - error-handling
  - supervisor
  - json-contract
  - cross-language
  - u6
---

# Daemon start-failures must be raised before `EVENT_STARTED`

## Problem

On the daemon transport, a recording start that lacked the Screen Recording
grant produced a **confusing two-phase failure**, not a clean one:

1. The engine worker emits `EVENT_STARTED` **before** running its own permission
   preflight (`src/screencap/cli/__init__.py` `_engine_worker_cmd` emits
   `started`, *then* calls `run_recording_worker`).
2. `Supervisor.spawn`'s `_wait_on_subscription(EVENT_STARTED)` therefore
   **succeeds**, so `recording.start` returns **200 OK** with a session id.
3. Only *then* does the worker run its Screen-Recording check
   (`session.py` `run_recording_worker` → `emit_event(permission_lost)` →
   `raise SystemExit(3)`).
4. `_handle_engine_exit` synthesizes `engine_crashed` + `recording_finalized`
   over the event stream.

Net effect: the caller gets a **successful start that instantly collapses into an
async crash**, with no structured error on the `recording.start` call itself. Two
gaps compound it: the daemon path preflighted **Screen Recording only**
(Accessibility / Input Monitoring were never start-gated), and the failure was
delivered as an asynchronous event rather than a result on the call.

> Note on a misleading prior: the "silent `SystemExit` into an empty `serve.log`"
> description belongs to the **standalone-CLI** path, *not* the app-connected
> daemon path. On the daemon path it is "200 OK, then async crash." Verify the
> actual code flow before trusting a prose failure description.

## Symptoms

- `recording.start` returns `200 OK`, then the recording dies via async
  `permission_lost` / `engine_crashed` / `recording_finalized`.
- The app surfaces a generic crash instead of "you're missing Screen Recording."
- Only Screen Recording was ever start-gated on the daemon path.

## What didn't work / traps

- **Letting the engine `SystemExit` and classifying the supervisor's wait
  timeout as a permission failure.** It waits the full `startup_timeout` and
  still produces synthesized crash events — slow and ambiguous (it's not even a
  timeout in the daemon case, since `EVENT_STARTED` *succeeds* first).
- **Gating start with an in-process `DarwinPlatform.is_*_enabled()` check in the
  daemon.** The daemon is long-lived, so an in-process Quartz check returns TCC
  state cached at daemon launch (R9). It would block **every** start forever
  after a post-launch grant — the exact bug the feature exists to fix. The
  preflight **must** use the fresh-subprocess probe (U1), whose first in-process
  check in a freshly spawned process reads live state.

## Resolution

Move the fatal precondition check **into the supervisor, before the success
signal**:

- In `Supervisor.spawn`, **before** claiming the engine spawn / before
  `_wait_on_subscription`, run U1's fresh-subprocess probe and raise a typed
  `PermissionRequiredError(DaemonAPIError)` carrying the missing-permission list,
  mapped to a 4xx so it routes through `_api_error_response` (not the generic
  `except Exception` 500). The app decodes it via the existing
  `envelopeError(code:rawBody:)` path and reuses the `handlePermissionLost`
  presentation — no new error surface.
- **Single source of truth.** Because the daemon now rejects *before* the worker
  spawns, the worker's `EVENT_PERMISSION_LOST` cannot double-fire for the same
  attempt. The worker's in-process preflight stays only as a backstop for the
  **standalone-CLI** transport and for **mid-recording** revocation.
- **Breadth.** Hard-block start on **Screen Recording only** (the one permission
  fatal to capture); Accessibility / Input Monitoring are warn-and-proceed. When
  the block does trigger, the reported `missing[]` still lists *every* denied
  required permission, so the app can surface them all.
- **Latency budget.** Reuse the most recent `daemon.info` probe result (U2's
  cache) instead of a second fresh ~5s spawn, and run the preflight **before**
  `pidfile.claim_lock` and **off the event loop** (`asyncio.to_thread`) — a fresh
  spawn inside the lock would widen the lock-contended window and risk exceeding
  the app's 10s `recordingStart` client timeout.

## Lesson

When a worker emits a "started" signal *before* its own fatal preflight, the
supervising request returns success and the real failure degrades into an
unobservable async crash. **Gate fatal preconditions in the supervisor before the
success signal, and surface them as a typed, synchronous error on the originating
request.** And in a long-lived daemon, any "is it granted now?" check that must
reflect *live* OS state has to be a fresh subprocess — an in-process API call
caches its answer at process launch.

## Related

- `docs/solutions/integration-issues/review-data-nullable-timing-swift-consumer-2026-06-01.md`
  — the Python↔Swift envelope playbook (decode optional-in-contract fields as
  optional; reserve the failure path for the explicit `ok:false` discriminator).
- `docs/solutions/runtime-errors/macos-tcc-per-process-cache-quit-and-relaunch.md`
  — why an in-process TCC check in a long-lived process is stale.
- `docs/solutions/runtime-errors/capture-health-nonscreen-attribution-terminal-stop-2026-06-01.md`
  — why only a Screen Recording denial is fatal to capture (the breadth rule).
- Plan: `docs/plans/2026-06-05-003-feat-daemon-tcc-permission-visibility-onboarding-plan.md` (U6, R4/R7).
