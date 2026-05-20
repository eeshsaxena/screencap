---
title: "refactor: Split RecorderController into state machine and transport/services (SCR-58)"
type: refactor
status: completed
date: 2026-05-19
completed: 2026-05-20
---

# refactor: Split RecorderController into state machine and transport/services (SCR-58)

## Summary

Extract `RecorderController`'s transport, stop-policy, permission-watchdog, and AppKit presentation concerns into focused collaborator units behind protocols, leaving a smaller orchestrator that owns the SwiftUI-facing `@Published` surface. Public API consumed by views and `AppDelegate` is preserved verbatim; existing tests pass unchanged, and each extracted unit gets focused coverage.

---

## Problem Frame

`macos/ScreenCap/Controllers/RecorderController.swift` is now 926 lines (grew past the 860 noted in SCR-58) and owns: UI-facing recording state, daemon transport probe and event-stream consumption with reconnect/backoff, CLI fallback subprocess spawning and stderr parsing, stop/quit policy (30s in-app, 300s Cmd+Q with SIGKILL escape), permission watchdog (`Timer` + `NSWorkspace` observer), schema-drift logging, and two `NSAlert` modal flows. This is architecture debt, not a user-visible bug — but it makes every future macOS feature riskier (any new responsibility lands in the same file, every new contributor reads 900 lines before touching it) and prevents focused unit tests of the state machine and reconnect logic.

See [origin Linear ticket SCR-58](https://linear.app/zk-email/issue/SCR-58/split-recordercontroller-into-state-machine-and-transportservices).

---

## Requirements

- R1. `RecorderController` is reduced to a small orchestrator/store with explicit collaborator dependencies; its file no longer hosts transport, process, stop-policy, watchdog, or AppKit-modal code.
- R2. The public API consumed by SwiftUI views and `AppDelegate` is preserved with no view-side changes required (`state`, `lastError`, `matrixDisclosure`, `transport`, `daemonProbeCompleted`, `schemaMismatchDetected`, `quitProgressSecondsRemaining`, `start(name:)`, `stop()`, `probeDaemon()`, `confirmQuitWhileRecording()`, `dismissMatrixDisclosure()`, `bindIndex(_:)`, `bindPermissions(_:)`, `reloadDaemon()`, `smokeStatus()`).
- R3. Existing tests in `macos/ScreenCapTests/RecorderControllerTests.swift` and `macos/ScreenCapTests/RecorderControllerDaemonTests.swift` pass unchanged (no production-side `#if DEBUG` shim removed without an equivalent injection point).
- R4. Each extracted unit (state machine, daemon session service, CLI recorder service, stop policy coordinator, permission watchdog + alert presenter) is independently exercised by focused tests that do not require the full `RecorderController`.
- R5. Behavior is preserved exactly — no new event types, no new error wording, no changed timeouts, no changed FIFO dispatch ordering, no changed cursor-replay semantics. Inline rationale comments carry forward to the unit that now owns each behavior.

---

## Scope Boundaries

- No changes to `CLIClient`, `DaemonClient`, `PermissionController`, `RecordingsIndex`, or the `RecorderEventLine` / `CLIStatus` / `PrivacyMatrixDisclosure` / `RecordingState` / `RecorderTransport` public types.
- No changes to view files (`MenuBarMenu.swift`, `RecordingBanner.swift`, `MainWindow.swift`, `CalendarView.swift`, `PrivacyMatrixDisclosureView.swift`, `FirstRunPermissionsView.swift`).
- No changes to `AppDelegate.applicationShouldTerminate` flow or `ScreenCapApp.swift` wiring.
- No new event types or stderr contract changes (the `_stderr_events.py` schema-v1 contract is preserved).
- No changes to AppKit modal copy, button labels, timeouts (30s / 300s), backoff curve, or SIGKILL behavior.
- No migration off `Timer` to Combine / async sequences for the elapsed/watchdog timers.
- No removal of the CLI fallback transport (that decision was already deferred in the daemon Phase 2 plan).

### Deferred to Follow-Up Work

- Timer → async-sequence migration for elapsed/watchdog: tracked separately if a future ticket establishes the value.
- Splitting `RecorderController` further (e.g., one `@EnvironmentObject` per concern, view-side store decomposition): out of scope for SCR-58; revisit only if view code starts feeling cramped.
- Removing the `#if DEBUG` test shim and replacing with protocol injection at every seam: the new collaborator protocols cover most of what those shims do, but a few (`_testHandleStderrLine`, `_testHandleProcessTerminated`) remain useful for through-the-controller integration coverage and stay until the focused unit tests displace them.

---

## Context & Research

### Relevant Code and Patterns

- [macos/ScreenCap/Controllers/RecorderController.swift](macos/ScreenCap/Controllers/RecorderController.swift) — current 926-line controller; every responsibility called out in the ticket lives here.
- [macos/ScreenCap/Controllers/DaemonInstallController.swift](macos/ScreenCap/Controllers/DaemonInstallController.swift) — establishes the protocol-injected-collaborator pattern this refactor extends. Defines `DaemonRegistrationService` and `DaemonProbe` as `@MainActor protocol`s with live implementations (`SMAppServiceRegistration`, `LiveDaemonProbe`) and test substitutes. **This is the precedent to mirror.**
- [macos/ScreenCap/Controllers/QuitProgressCountdown.swift](macos/ScreenCap/Controllers/QuitProgressCountdown.swift) — already-extracted helper that demonstrates the "free-function/enum with injectable sleep" shape we want for the stop-policy coordinator's one-shot await.
- [macos/ScreenCap/Controllers/CLIClient.swift](macos/ScreenCap/Controllers/CLIClient.swift) — collaborator the CLI recorder service wraps; its `SpawnedProcess` handle, `spawn(args:onStderrLine:onTerminated:)`, `runDetached(["stop"])`, and `runJSON` are the API surface the service consumes.
- [macos/ScreenCap/Controllers/DaemonClient.swift](macos/ScreenCap/Controllers/DaemonClient.swift) — collaborator the daemon session service wraps. The `subscribe(sinceCursor:)` AsyncThrowingStream, `daemonInfo()`, `sessionSnapshot()`, `recordingStart()`, `recordingStop()` are the API surface.
- [macos/ScreenCapTests/RecorderControllerDaemonTests.swift](macos/ScreenCapTests/RecorderControllerDaemonTests.swift) — uses `UnixHTTPTestServer` to mock the daemon socket. Existing five tests exercise the daemon transport end-to-end; they should keep passing without changes by going through the orchestrator.
- [macos/ScreenCapTests/RecorderControllerTests.swift](macos/ScreenCapTests/RecorderControllerTests.swift) — exercises stderr-line handling and process-termination handling via `_testHandleStderrLine` / `_testHandleProcessTerminated` shims.

### Institutional Learnings

- [docs/solutions/ui-bugs/swiftui-windowgroup-vs-window-singleton-scene-2026-05-18.md](docs/solutions/ui-bugs/swiftui-windowgroup-vs-window-singleton-scene-2026-05-18.md) — recent SwiftUI scene wiring; informs the constraint that `@EnvironmentObject RecorderController` must remain a single store at the app-scene level (no splitting into multiple environment objects in this refactor).
- [docs/solutions/integration-issues/macos-foundation-process-pipe-pitfalls.md](docs/solutions/integration-issues/macos-foundation-process-pipe-pitfalls.md) — the `Process` / `Pipe` pitfalls already encoded in `CLIClient` and the existing controller's stderr-dispatch FIFO comment. The extracted CLI recorder service must preserve the GCD-vs-Task ordering rationale (comments on `RecorderController.swift:295-305`).
- [docs/solutions/runtime-errors/eventbus-late-listener-replay-2026-05-12.md](docs/solutions/runtime-errors/eventbus-late-listener-replay-2026-05-12.md) — the cursor-replay rationale behind `pendingStartCursor`. The daemon session service must carry this forward unchanged.

### External References

- Not used. Refactor is bounded to a single Swift file; local patterns are sufficient.

---

## Key Technical Decisions

- **Five extracted collaborators, not six**: the SCR-58 "Expected direction" lists six. The permission watchdog (one `Timer` + one `NSWorkspace` observer + a 14-line check function) and AppKit presentation (two `NSAlert` flows) are each too small to merit a standalone unit; they are extracted as two small collaborators in the same implementation step. Rationale: each extraction must add testability or independent reasoning; an `NSAlert`-wrapping protocol does that; a "permission watchdog component" of three short methods does not, but a protocol-injectable `PermissionWatchdog` does (the timer setup is what we want to mock in tests). Keeping them as separate small files is fine; pairing them into one implementation unit (U2) keeps the plan honest about how much engineering each one is.
- **Shell stays as a `@MainActor ObservableObject` with `@Published` state**: extracted services do not own `@Published` properties. They report state changes back via injected callbacks (e.g., `onEvent: (RecorderEventLine) -> Void`, `onProcessTerminated: (Int32) -> Void`) the orchestrator wires up. Rationale: SwiftUI views bind to `RecorderController` exclusively; spreading `@Published` across multiple objects forces `@ObservedObject` chains in every view and breaks R2.
- **Protocol-based seams for testability**: each collaborator exposes a `@MainActor protocol` (or non-isolated when the work is naturally off-actor, with `@Sendable` callbacks). Defaults to the live implementation in the controller's initializer; tests inject fakes. Mirrors the `DaemonRegistrationService` / `DaemonProbe` pattern.
- **State machine is a value-type, no `@MainActor`, no Combine**: the transition logic (handling the 9 stderr event types and the `recording_failed` / `started` / `stopped` / `recording_finalized` paths) is pure mapping from `(currentState, event)` to `(newState, effects)`. Effects are values the controller acts on. Rationale: the most valuable test target in this file is "given state X and event Y, the machine moves to state Z" — that test should not need a `@MainActor` or any of the surrounding infrastructure.
- **One-shot await (`waitForOneShot`) moves into the stop-policy coordinator**: it is intrinsically a stop-policy concern (timeout race + `awaitingFinalized` / `awaitingStopped` continuations). Rationale: every caller of `waitForOneShot` is in the stop policy; the abstraction belongs with its only client.
- **No new schema fields, no new public types except the collaborator protocols**: any structural type that views see today (`RecordingState`, `RecorderTransport`, `PrivacyMatrixDisclosure`, `CLIStatus`, `RecorderEventLine`) stays put. New types are internal to the controller package.
- **File-per-collaborator**: each extracted unit lands at `macos/ScreenCap/Controllers/Recorder<Name>.swift`. Rationale: searchability and matches the existing controller folder layout.

---

## Open Questions

### Resolved During Planning

- **Should `RecordingState` move with the state machine?**: yes. It is the state machine's domain type and has no SwiftUI dependency. Keeping it in a new `RecordingStateMachine.swift` keeps related code colocated. `@EnvironmentObject` views import the same module, so the move is source-compatible.
- **How are `awaitingFinalized` / `awaitingStopped` continuations exposed to the daemon and CLI paths?**: the stop-policy coordinator owns the continuation arrays and exposes `resolveFinalized(_:)` / `resolveStopped(_:)` methods. The state machine returns these as effect values; the controller routes them.
- **Does `confirmQuitWhileRecording()` move?**: no. It returns `NSApplication.TerminateReply` synchronously and is called from `AppDelegate.applicationShouldTerminate`, which is part of the controller's public surface (R2). The NSAlert it presents is delegated to the alert presenter; the orchestration stays.

### Deferred to Implementation

- Exact closure signature for daemon/CLI service callbacks (`(RecorderEventLine) -> Void` vs `@Sendable` variants) — will surface during implementation depending on Swift 6 strict concurrency diagnostics.
- Whether the daemon session service exposes the reconnect loop as a single `attach()` async method or as a more granular streaming API — pick whichever lets the existing five daemon tests pass with the smallest test-side change.
- Whether `MainActor.assumeIsolated` callsites need to migrate to the new collaborators or can be eliminated by tightening actor boundaries — likely some of each; decide per call-site at implementation time.

---

## High-Level Technical Design

> *This illustrates the intended approach and is directional guidance for review, not implementation specification. The implementing agent should treat it as context, not code to reproduce.*

### Collaborator graph

```
                ┌─────────────────────────────────────────────┐
                │            RecorderController                │
                │   (@MainActor ObservableObject orchestrator) │
                │                                              │
                │  @Published state, lastError, transport,     │
                │  matrixDisclosure, quitProgressSecondsRemain │
                └──┬──────────┬─────────┬─────────┬─────────┬──┘
                   │          │         │         │         │
                   ▼          ▼         ▼         ▼         ▼
              ┌─────────┐ ┌────────┐ ┌────────┐ ┌────────┐ ┌─────────┐
              │ State   │ │Daemon  │ │ CLI    │ │ Stop   │ │Watchdog │
              │Machine  │ │Session │ │Recorder│ │ Policy │ │+ Alert  │
              │(value)  │ │Service │ │Service │ │Coord.  │ │Presenter│
              └─────────┘ └───┬────┘ └───┬────┘ └────────┘ └─────────┘
                              │          │
                              ▼          ▼
                       ┌────────────┐ ┌────────────┐
                       │DaemonClient│ │ CLIClient  │
                       │ (unchanged)│ │ (unchanged)│
                       └────────────┘ └────────────┘
```

### State machine sketch

The state machine is a value type with one method shaped roughly like:

```
struct RecordingStateMachine {
    var state: RecordingState   // .idle | .starting | .recording | .stopping
    var pendingStartCursor: Int?
    var recordingStartedAt: Date?

    enum AwaitKind { case finalized, stopped }

    enum Effect {
        case startElapsedTimer
        case stopElapsedTimer
        case armPermissionWatchdog
        case disarmPermissionWatchdog
        case resolveAwaiting(AwaitKind, success: Bool)
        case surfaceError(String)
        case setMatrixDisclosure(PrivacyMatrixDisclosure)
        case clearStartCursor
        case refreshIndex
    }

    // Pure: maps (currentState, event) → (newState, [Effect]).
    mutating func handle(event: RecorderEventLine) -> [Effect]
    mutating func observeStarted(at: Date) -> [Effect]
    mutating func enterStarting(transport: RecorderTransport) -> [Effect]
    mutating func enterStopping(quitting: Bool) -> [Effect]
    mutating func processTerminated(exitCode: Int32) -> [Effect]
}
```

The controller calls `machine.handle(event)`, applies effects to `@Published` state and side-effects (timers, awaits), and never inspects the machine's internals directly.

### Start flow (daemon transport) — sequence

```
View          Controller        StateMachine     DaemonSessionSvc    DaemonClient
 │  start()       │                  │                  │                  │
 ├───────────────▶│ enterStarting()  │                  │                  │
 │                ├─────────────────▶│                  │                  │
 │                │   [.idle→.start] │                  │                  │
 │                │◀─────────────────┤                  │                  │
 │                │                  │                  │                  │
 │                │ start(name)      │                  │                  │
 │                ├─────────────────────────────────────▶                  │
 │                │                  │                  │ recordingStart() │
 │                │                  │                  ├─────────────────▶│
 │                │                  │                  │◀──── cursor ─────┤
 │                │                  │                  │ subscribe(cursor)│
 │                │                  │                  ├─────────────────▶│
 │                │     onEvent(.started)               │                  │
 │                │◀───────────────────────────────────┤                  │
 │                │ handle(.started) │                  │                  │
 │                ├─────────────────▶│                  │                  │
 │                │   .starting→.rec │                  │                  │
 │  state observed                   │                  │                  │
 │◀───────────────┤                  │                  │                  │
```

---

## Implementation Units

### U1. Extract `RecordingStateMachine` (value type)

**Goal:** Move the state-transition logic — including `RecordingState`, stderr-event handling, process-termination handling, schema-drift logging, and the `pendingStartCursor` / `recordingStartedAt` ledger — into a pure-value state machine that maps events to state + effects without any UI or concurrency dependency.

**Requirements:** R1, R4, R5

**Dependencies:** None

**Files:**
- Create: `macos/ScreenCap/Controllers/RecordingStateMachine.swift` (also home for the moved `RecordingState` enum and `DaemonErrorCode` constants)
- Modify: `macos/ScreenCap/Controllers/RecorderController.swift` (delegate `handleRecorderEvent`, `handleProcessTerminated`, and state-flag computations to the machine; keep the published `state` mirroring `machine.state`)
- Test: `macos/ScreenCapTests/RecordingStateMachineTests.swift`

**Approach:**
- Define `struct RecordingStateMachine` with internal `state`, `pendingStartCursor`, `recordingStartedAt` storage.
- Define an `Effect` enum the controller acts on (elapsed-timer arm/disarm, watchdog arm/disarm, await-resolve, error surface, matrix disclosure, index refresh).
- Port the `switch event.type` from `RecorderController.handleRecorderEvent` into `machine.handle(event:)` returning `[Effect]`. Preserve every guard (e.g., `case .starting = state` for the `started` event), every rationale comment, and the schema-drift warning log.
- Port `handleProcessTerminated` exit-code branching (0/130/143 keep prior warning; 2/3/4/other set `lastError`) into `machine.processTerminated(exitCode:)`.
- The controller reduces to: `let effects = machine.handle(event); apply(effects)`.

**Patterns to follow:**
- `DaemonInstallController.State` enum-with-associated-values style (uses an `enum` + raw-value subtype for failure reasons).
- The `QuitProgressCountdown` shape — value-/free-function logic with no MainActor isolation.

**Test scenarios:**
- Happy path: `.idle` + `enterStarting(.daemon)` → state `.starting`, no effects beyond what daemon path needs.
- Happy path: `.starting` + `started` event → state `.recording(elapsed: 0)`, effect `startElapsedTimer`, `pendingStartCursor` cleared.
- Happy path: `.recording` + `recording_finalized` (no `force_stopped`) → effect `resolveAwaiting(.finalized, success: true)`, effect `refreshIndex`, no error.
- Edge case: `.recording` + `recording_finalized` with `force_stopped=true` → effect `surfaceError("Recording stopped, but some data may not have uploaded. Run `screencap upload` to retry.")` (exact message preserved), plus the finalized resolve.
- Edge case: `.recording` + duplicate `started` event → state unchanged (the `case .starting = state` guard).
- Edge case: `.starting` + `recording_failed` (reason "engine crashed") → state `.idle`, effect `surfaceError("engine crashed")`, both await arrays resolved (finalized=true, stopped=false), `pendingStartCursor` cleared.
- Edge case: unknown event type → no state change, debug log only (no effects).
- Edge case: schema drift (`schema_version` != 1) → warning logged, event still processed.
- Process termination — clean exit (0/130/143) preserves prior `lastError`; exit code 2 sets "ScreenCap is already recording."; exit code 3 sets permission revoke message; exit code 4 sets disk-full message; other sets generic "Recorder exited with code N."
- Edge case: `permission_lost` event yields the effects the controller needs to drive the modal flow (effect carries permission name).
- Edge case: `matrix_disclosure_required` event yields `setMatrixDisclosure` effect carrying the `changes` and `optOutCommandExamples`.

**Verification:**
- State-machine tests pass without spinning up `RecorderController`, AppKit, Combine, or any test server.
- All existing `RecorderControllerTests.swift` assertions about stderr events still pass when run through the controller.

---

### U2. Extract `PermissionWatchdog` and `RecorderAlertPresenter`

**Goal:** Move the `Timer`+`NSWorkspace` observer watchdog (`startPermissionWatchdog`, `stopPermissionWatchdog`, `checkPermissionsDuringRecording`) and the two `NSAlert` flows (Cmd+Q confirmation in `confirmQuitWhileRecording`, permission-lost alert in `handlePermissionLost`) into two small protocol-backed collaborators.

**Requirements:** R1, R2, R4, R5

**Dependencies:** None

**Files:**
- Create: `macos/ScreenCap/Controllers/PermissionWatchdog.swift` (protocol + live implementation)
- Create: `macos/ScreenCap/Controllers/RecorderAlertPresenter.swift` (protocol + live implementation)
- Modify: `macos/ScreenCap/Controllers/RecorderController.swift` (inject collaborators, remove inline timer / NSAlert code)
- Test: `macos/ScreenCapTests/PermissionWatchdogTests.swift`
- Test: `macos/ScreenCapTests/RecorderAlertPresenterTests.swift` (lightweight — most coverage of the alert flow remains in `RecorderControllerTests`)

**Approach:**
- `PermissionWatchdog` protocol exposes `start(check: @escaping @MainActor () -> Void)` and `stop()`. Live implementation owns the `Timer` + `NSWorkspace` observer. The "should I check?" gating (transport == cliFallback, state == .recording) stays in the orchestrator's check closure.
- `RecorderAlertPresenter` protocol exposes `confirmStopAndQuit() -> NSApplication.TerminateReply` (returns user choice; orchestrator handles the state transition) and `presentPermissionLost(permission: String, openSettings: () -> Void)`. Live implementation builds the `NSAlert`s with the existing copy.
- The Cmd+Q re-entry guard (`if case .stopping(quitting: true) = state { return .terminateLater }`) stays in the orchestrator because it touches state.

**Patterns to follow:**
- `DaemonRegistrationService` + `SMAppServiceRegistration`: `@MainActor protocol` + final class live implementation in the same file, defaulted in the consumer's initializer.

**Test scenarios:**
- Watchdog: `start(check:)` invokes the check closure on the timer's fire interval (use injected sleep/clock if convenient; otherwise drive the timer via `RunLoop.main.run(until:)`).
- Watchdog: `stop()` invalidates the timer and removes the observer; subsequent `NSWorkspace.didActivateApplicationNotification` does not invoke the check.
- Watchdog: re-calling `start` while already running invalidates the previous timer (no leak).
- Alert presenter: `confirmStopAndQuit` is hard to test directly (modal is synchronous). Cover via a `FakeAlertPresenter` in `RecorderControllerTests` that returns each `TerminateReply` value, and assert the orchestrator transitions accordingly (covers Cmd+Q first-button → `.stopping(quitting:true)` + countdown, cancel → `.terminateCancel`, keep-recording → `.terminateCancel`).

**Verification:**
- The controller no longer references `NSAlert` or `Timer` directly.
- Existing `testForceStoppedWarningSurvivesCleanProcessTermination`, `testDaemonTransportPermissionWatchdogIgnoresAppProcessPermissions`, and `testCLIFallbackStillBlocksStartOnAppProcessPermissions` pass.

---

### U3. Extract `StopPolicyCoordinator`

**Goal:** Move the stop-policy logic (`runStop`, `runStopViaDaemon`, `awaitFinalizedEvent`, `awaitStoppedEvent`, `waitForOneShot`, `tickQuitProgress`) and the awaiting-continuation arrays into a dedicated coordinator that exposes "start an in-app stop" and "start a Cmd+Q stop" entry points.

**Requirements:** R1, R4, R5

**Dependencies:** U1 (state machine returns the resolve-await effects), U2 (presenter for the Cmd+Q failure path)

**Files:**
- Create: `macos/ScreenCap/Controllers/StopPolicyCoordinator.swift`
- Modify: `macos/ScreenCap/Controllers/RecorderController.swift` (delegate `stop()` and post-confirm Cmd+Q flow to the coordinator)
- Test: `macos/ScreenCapTests/StopPolicyCoordinatorTests.swift`

**Approach:**
- Coordinator is `@MainActor` and holds the `awaitingFinalized` / `awaitingStopped` continuation arrays. Exposes `resolveFinalized(_: Bool)` / `resolveStopped(_: Bool)` so the orchestrator can flush them when the state machine emits the corresponding effect.
- Exposes `runStop(quitting: Bool, transport: RecorderTransport)` → `async`. The coordinator depends on injected `sendStopSignal: () async throws -> Void` (wraps `CLIClient.runDetached(["stop"])` or `DaemonClient.recordingStop(...)`); the orchestrator wires the right one based on transport at call time.
- Preserve the FIFO-ordering rationale, the 30s vs 300s timeout, the SIGKILL escape (orchestrator passes a `killProcess: () -> Void` closure that the coordinator invokes if the 300s wait fails and the CLI process is still alive), and the `NSApp.reply(...)` call sequencing.
- `QuitProgressCountdown` continues to be the sleep driver during a Cmd+Q wait; it stays untouched.

**Patterns to follow:**
- `QuitProgressCountdown`'s injectable-sleep shape for the timeout race.
- Existing `waitForOneShot`'s "resumed flag + cancel timeout" idiom; carry verbatim including the residual-cleanup comment.

**Test scenarios:**
- Happy path (in-app): `runStop(quitting: false, ...)` resolves once `resolveFinalized(true)` is called within 30s; reports success; orchestrator transitions to `.idle`.
- Edge case (in-app timeout): `runStop(quitting: false, ...)` with no `resolveFinalized` call resolves with `success=false` after the timeout; orchestrator sets the "still finalizing" message.
- Edge case (Cmd+Q success): `runStop(quitting: true, ...)` resolves once `resolveStopped(true)` is called; orchestrator calls `NSApp.reply(true)` and clears the countdown.
- Edge case (Cmd+Q timeout, CLI alive): `runStop(quitting: true, ...)` with no `resolveStopped` after 300s invokes the injected `killProcess` closure and surfaces "Stop timed out after 5 minutes; recorder force-killed."
- Edge case (Cmd+Q timeout, CLI already exited): same as above but `killProcess` is not invoked (orchestrator checks `isRunning && pid > 0`).
- Edge case (stop signal fails): `sendStopSignal` throws → coordinator does not start a wait, surfaces the error, returns to caller so orchestrator can restore `.recording(elapsed:)` and call `NSApp.reply(false)` if `quitting`.
- Edge case (success cancels timeout): `resolveFinalized(true)` mid-wait cancels the sleep task so the coordinator does not wait the full duration.

**Verification:**
- Existing tests covering in-app stop and Cmd+Q stop continue to pass.
- The controller's `stop()` method shrinks to: validate guard, set state via state machine, delegate to coordinator.

---

### U4. Extract `CLIRecorderService`

**Goal:** Move the CLI fallback recording lifecycle (`startViaCLI`, `handleStderrLine`, `handleProcessTerminated` invocation wiring, `SpawnedProcess` retention) into a dedicated service.

**Requirements:** R1, R4, R5

**Dependencies:** U1 (state machine consumes stderr events and exit codes)

**Files:**
- Create: `macos/ScreenCap/Controllers/CLIRecorderService.swift`
- Modify: `macos/ScreenCap/Controllers/RecorderController.swift` (delegate CLI start + own `SpawnedProcess`-derived `pid` lookup for the SIGKILL path through a small accessor)
- Test: `macos/ScreenCapTests/CLIRecorderServiceTests.swift`

**Approach:**
- Service exposes `start(args: [String], onEvent: @escaping @MainActor (RecorderEventLine) -> Void, onTerminated: @escaping @MainActor (Int32) -> Void) throws` and `currentProcess: SpawnedProcess?` (read by U3 for the SIGKILL guard).
- Owns the JSON parsing of stderr lines (the current `handleStderrLine` body: trim, prefix-check, decode) — emits decoded `RecorderEventLine` to `onEvent`.
- Preserves the `DispatchQueue.main.async { MainActor.assumeIsolated { ... } }` dispatch idiom and its FIFO rationale comment verbatim.
- The "should we check permissions before spawn?" gate stays in the orchestrator (it depends on `PermissionController` which the service should not couple to).

**Patterns to follow:**
- Existing inline `CLIClient.spawn(...)` call shape; preserve the `onStderrLine` and `onTerminated` closure structure unchanged.

**Test scenarios:**
- Happy path: `start` succeeds → service retains a `SpawnedProcess`; stderr JSON line is parsed and delivered to `onEvent` as a decoded `RecorderEventLine`.
- Edge case: stderr line that is not JSON (no `{` prefix) → silently dropped, `onEvent` not called.
- Edge case: stderr line that is malformed JSON → silently dropped (matches current behavior).
- Edge case: process termination → `onTerminated(exitCode)` called once.
- Edge case: `CLIClient.spawn` throws → service surfaces the error; no `currentProcess` retained.
- Integration: the orchestrator wires events into the state machine; covered end-to-end by existing `RecorderControllerTests` (the `_testHandleStderrLine` shim keeps working by routing into the service's parser via the orchestrator).

**Verification:**
- The controller no longer holds a `spawn: CLIClient.SpawnedProcess?` field directly; it asks the CLI service for the current process when SIGKILL is needed.
- Existing tests pass.

---

### U5. Extract `DaemonSessionService`

**Goal:** Move the daemon transport lifecycle (`probeDaemon`, `syncDaemonSnapshot`, `startViaDaemon`, `runStopViaDaemon` daemon-side, `attachDaemonEventStream`, `consumeDaemonEvents` with reconnect/backoff/cursor handling, `handleDaemonOperationFailure`, `reloadDaemon`) into a dedicated service.

**Requirements:** R1, R4, R5

**Dependencies:** U1 (state machine handles incoming events), U3 (stop policy invokes `recordingStop` via the service)

**Files:**
- Create: `macos/ScreenCap/Controllers/DaemonSessionService.swift`
- Modify: `macos/ScreenCap/Controllers/RecorderController.swift` (delegate probe and start-via-daemon; receive event stream callbacks)
- Test: `macos/ScreenCapTests/DaemonSessionServiceTests.swift`

**Approach:**
- Service owns the `daemonEventTask: Task<Void, Never>?` and the cursor handling (`pendingStartCursor`). Exposes `probe() async -> ProbeResult` (returns `.daemon` / `.cliFallback(reason:)` / `.schemaMismatch`), `start(name:) async throws -> Cursor`, `stop(force:) async throws`, `attachEventStream(sinceCursor:)`, `cancel()`, and a snapshot-sync entry point used by `probe`.
- Reconnect/backoff loop (`consumeDaemonEvents`) moves into the service. Emits decoded events to an injected `onEvent: @escaping @MainActor (RecorderEventLine) -> Void` callback (matching U4's CLI service signature). Surfaces "lost contact" / "another process is recording" / "recording ended" / "daemon recovering" as typed result values the orchestrator translates into `lastError` via the state machine effect channel.
- `handleDaemonOperationFailure`'s policy (schemaMismatch → cliFallback; socketUnavailable/connectionFailed → cliFallback + optional fallback closure; envelopeError → tailored message) moves into the service and returns a `DaemonFailureOutcome` enum the orchestrator translates.
- `reloadDaemon`'s `/bin/launchctl kickstart -kp` invocation stays here too — it is a daemon-transport concern.
- The `NotificationCenter` observer for `.screenCapDaemonInstalledAndRunning` stays in the orchestrator (it is the wiring layer that decides "probe again"); the service exposes the `probe()` method the observer calls.

**Patterns to follow:**
- `DaemonClient`'s existing `AsyncThrowingStream` API; service consumes it without re-shaping.
- The reconnect-with-capped-exponential-backoff comment block lifts verbatim; this is the single highest-value rationale in the file.

**Test scenarios:**
- Happy path: `probe()` against a healthy mock daemon returns `.daemon`; subsequent `start(name:)` returns a cursor; `attachEventStream(sinceCursor:)` delivers the `started` event.
- Edge case (schema mismatch): `probe()` against a daemon with `api_schema_version: 99` returns `.schemaMismatch`; orchestrator flips `schemaMismatchDetected` and `transport` to `.cliFallback`.
- Edge case (foreign claimant): `probe()` against a daemon where snapshot reports `is_recording=true, daemon_owned=false` returns a result that drives the orchestrator's "Another process is recording." message; state stays `.idle`.
- Edge case (cursor_unknown 410): the reconnect loop refetches snapshot and resubscribes; cursor flips from `pendingStartCursor` to the fresh snapshot cursor.
- Edge case (stream drop before `started`): reconnect uses the original start cursor (not the snapshot cursor) so the replay boundary survives.
- Edge case (10 consecutive failures): service emits "Lost contact with daemon" outcome; orchestrator transitions to `.idle`.
- Edge case (`_close` reason="shutdown"): consume loop terminates without surfacing an error.
- Edge case (`startViaDaemon` throws schemaMismatch / socketUnavailable / connectionFailed): outcome carries fallback signal; orchestrator invokes CLI fallback.

**Verification:**
- All five existing `RecorderControllerDaemonTests` pass unchanged (they exercise the orchestrator end-to-end, which routes through the new service).
- The controller's `probeDaemon()` method shrinks to: await `service.probe()`, apply the outcome.

---

### U6. Reduce `RecorderController` to orchestrator/store and update test shims

**Goal:** Final pass — `RecorderController.swift` now only contains `@Published` state, collaborator wiring, public API entry points that delegate to collaborators, and the effect-application loop that translates state-machine `Effect` values into property updates and collaborator calls.

**Requirements:** R1, R2, R3

**Dependencies:** U1, U2, U3, U4, U5

**Files:**
- Modify: `macos/ScreenCap/Controllers/RecorderController.swift` (final shrink)
- Modify: `macos/ScreenCapTests/RecorderControllerTests.swift` (only if a `#if DEBUG` shim must change shape; prefer keeping shims identical)
- Modify: `macos/ScreenCapTests/RecorderControllerDaemonTests.swift` (only if a `#if DEBUG` shim must change shape; prefer keeping shims identical)

**Approach:**
- Initializer takes optional collaborators with live defaults: `init(stateMachine: ... = .init(), daemonService: DaemonSessionService = LiveDaemonSessionService(), cliService: CLIRecorderService = LiveCLIRecorderService(), stopPolicy: StopPolicyCoordinator = .init(), watchdog: PermissionWatchdog = LivePermissionWatchdog(), alertPresenter: RecorderAlertPresenter = LiveRecorderAlertPresenter())`.
- The `@Published` properties become the orchestrator's only data. `state` mirrors `machine.state` after every effect application.
- Public API methods (`start`, `stop`, `probeDaemon`, `confirmQuitWhileRecording`, `dismissMatrixDisclosure`, `bindIndex`, `bindPermissions`, `reloadDaemon`, `smokeStatus`) delegate.
- The `#if DEBUG` extensions (`_testSetPresentation`, `_testHandleStderrLine`, `_testHandleProcessTerminated`, `_testSetTransport`, `_testCheckPermissionsDuringRecording`, `_testCancelDaemonTask`) stay; their bodies now route through the new collaborators (e.g., `_testHandleStderrLine` calls the CLI service's parser; `_testCancelDaemonTask` calls `daemonService.cancel()`).
- Target line count for the file: roughly 200–300 lines (down from 926). This is a *target*, not a hard cap.

**Patterns to follow:**
- `DaemonInstallController` as the shape reference for a controller-as-orchestrator.

**Test scenarios:**
- All existing `RecorderControllerTests.swift` tests pass unchanged.
- All existing `RecorderControllerDaemonTests.swift` tests pass unchanged.
- Smoke: launch the app via Xcode against a live daemon, start a recording, stop via Stop button, observe the `recording_finalized` flow completes and the recording appears in the index. (Manual; not a unit test.)
- Smoke: launch the app, start a recording, Cmd+Q, click Stop & Quit, observe the countdown and the clean quit.
- Smoke: launch the app with the daemon socket missing, start a recording (CLI fallback), stop via Stop button, observe completion.

**Verification:**
- `RecorderController.swift` line count is materially reduced (target band 200–300 lines).
- No view file is modified.
- `git grep -n "Timer\|NSAlert\|NSWorkspace\|CLIClient\.spawn\|DaemonClient\." -- macos/ScreenCap/Controllers/RecorderController.swift` returns no hits (every concern delegated).
- Full Xcode test suite passes.

---

## System-Wide Impact

- **Interaction graph:** views (`MenuBarMenu`, `RecordingBanner`, `MainWindow`, `CalendarView`, `FirstRunPermissionsView`, `PrivacyMatrixDisclosureView`) and `AppDelegate.applicationShouldTerminate` continue to talk to `RecorderController` exclusively. No new `@EnvironmentObject`s appear at app-scene level.
- **Error propagation:** state-machine `Effect.surfaceError` is the single channel through which messages reach `lastError`. Daemon and CLI services translate transport-specific errors into typed outcomes; the orchestrator routes outcomes to the machine. This is more disciplined than the current code without changing the user-visible result.
- **State lifecycle risks:** the `awaitingFinalized` / `awaitingStopped` arrays moving into the stop-policy coordinator must not orphan a continuation if the controller is destroyed mid-wait. The coordinator's `deinit` (or explicit `cancelAll` on tear-down) drains pending continuations with `success=false` so callers cannot deadlock.
- **API surface parity:** every `@Published` property and every public method on `RecorderController` retains the same name, signature, and observable semantics. Verified by grep against view and `AppDelegate` source.
- **Integration coverage:** five existing daemon tests use `UnixHTTPTestServer`. These cover the most important end-to-end path and remain the integration gate. Focused unit tests on the new units do not replace them; they complement.
- **Unchanged invariants:** `RecordingState`, `RecorderTransport`, `PrivacyMatrixDisclosure`, `CLIStatus`, `RecorderEventLine`, the stderr event schema version, daemon API schema version, and the `_stderr_events.py` contract on the Python side are unchanged. The `DaemonClient`, `CLIClient`, `PermissionController`, and `RecordingsIndex` classes are unchanged.

---

## Risks & Dependencies

| Risk | Mitigation |
|------|------------|
| Subtle behavior drift in stderr-event handling (most likely failure mode for any refactor of this kind) | U1 lands first with thorough enumerated unit tests for every event type *before* U4/U5 wire in. Run the full Xcode test suite after each unit lands. |
| FIFO ordering between stderr and termination dispatches breaks if a `Task { @MainActor }` slips in where `DispatchQueue.main.async` was used | Lift the FIFO rationale comment verbatim into the CLI service. Code review checklist item: "no `Task { @MainActor }` callbacks from `CLIClient.spawn` handlers." |
| `pendingStartCursor` semantics regress (cursor-unknown 410 path or stream-drop-before-started reconnect) | All four cursor-related daemon tests are end-to-end via `UnixHTTPTestServer`. Pass-or-fail on the existing suite is the gate. |
| `_testCancelDaemonTask` shim becomes a no-op after extraction, leaking event tasks in tests | The shim explicitly calls `daemonService.cancel()`. Verified by the existing daemon-test tearDown sequence. |
| Continuation leak from `waitForOneShot` becomes worse after extraction | Carry the residual-cleanup comment verbatim and consider the coordinator-deinit drain as part of U3. |
| Permission-watchdog re-arm-on-restart bug | `PermissionWatchdog.stop()` is called from `start()` to invalidate the previous timer; covered by U2 test. |
| Existing `_test*` shims need bigger changes than expected | If a shim becomes awkward to express through collaborators, prefer keeping the shim and exposing a narrow internal accessor; do not invent a new public API just for tests. |

---

## Documentation / Operational Notes

- No user-visible changes; no docs update.
- After the refactor lands, consider a short `docs/solutions/` entry capturing the protocol-injected-collaborator pattern as a reusable convention for future SwiftUI controllers in this app. Optional; punt if it doesn't feel load-bearing.

---

## Sources & References

- **Origin Linear ticket:** [SCR-58 — Split RecorderController into state machine and transport/services](https://linear.app/zk-email/issue/SCR-58/split-recordercontroller-into-state-machine-and-transportservices) (parent: SCR-13, project: MacOS, label: Improvement, priority: Medium)
- **Related code:**
  - [macos/ScreenCap/Controllers/RecorderController.swift](macos/ScreenCap/Controllers/RecorderController.swift)
  - [macos/ScreenCap/Controllers/DaemonInstallController.swift](macos/ScreenCap/Controllers/DaemonInstallController.swift) (pattern reference)
  - [macos/ScreenCap/Controllers/CLIClient.swift](macos/ScreenCap/Controllers/CLIClient.swift)
  - [macos/ScreenCap/Controllers/DaemonClient.swift](macos/ScreenCap/Controllers/DaemonClient.swift)
  - [macos/ScreenCap/Controllers/QuitProgressCountdown.swift](macos/ScreenCap/Controllers/QuitProgressCountdown.swift)
  - [macos/ScreenCapTests/RecorderControllerTests.swift](macos/ScreenCapTests/RecorderControllerTests.swift)
  - [macos/ScreenCapTests/RecorderControllerDaemonTests.swift](macos/ScreenCapTests/RecorderControllerDaemonTests.swift)
- **Prior plans (architectural context):**
  - [docs/plans/2026-05-08-002-feat-daemon-architecture-phase-2-plan.md](docs/plans/2026-05-08-002-feat-daemon-architecture-phase-2-plan.md)
- **Institutional learnings:**
  - [docs/solutions/runtime-errors/eventbus-late-listener-replay-2026-05-12.md](docs/solutions/runtime-errors/eventbus-late-listener-replay-2026-05-12.md)
  - [docs/solutions/integration-issues/macos-foundation-process-pipe-pitfalls.md](docs/solutions/integration-issues/macos-foundation-process-pipe-pitfalls.md)
  - [docs/solutions/runtime-errors/macos-tcc-per-process-cache-quit-and-relaunch.md](docs/solutions/runtime-errors/macos-tcc-per-process-cache-quit-and-relaunch.md)
