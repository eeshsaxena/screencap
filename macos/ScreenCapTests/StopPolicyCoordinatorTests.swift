import XCTest
@testable import ScreenCap

@MainActor
final class StopPolicyCoordinatorTests: XCTestCase {
    private struct StopSignalError: Error, LocalizedError {
        var errorDescription: String? { "stop signal could not dispatch" }
    }

    // MARK: - Happy paths

    func testInAppStopResolvesWhenFinalizedFires() async {
        let coordinator = LiveStopPolicyCoordinator()

        Task { @MainActor in
            // Give runStop a moment to register its continuation.
            try? await Task.sleep(nanoseconds: 20_000_000)
            coordinator.resolveFinalized(true)
        }

        let outcome = await coordinator.runStop(
            quitting: false,
            timeout: 5.0,
            sendStopSignal: { /* succeeds */ }
        )

        XCTAssertTrue(outcome.isCompleted)
    }

    func testCmdQStopResolvesWhenStoppedFires() async {
        let coordinator = LiveStopPolicyCoordinator()

        Task { @MainActor in
            try? await Task.sleep(nanoseconds: 20_000_000)
            coordinator.resolveStopped(true)
        }

        let outcome = await coordinator.runStop(
            quitting: true,
            timeout: 5.0,
            sendStopSignal: { }
        )

        XCTAssertTrue(outcome.isCompleted)
    }

    // MARK: - Timeouts

    func testInAppStopTimesOutWhenFinalizedNeverFires() async {
        let coordinator = LiveStopPolicyCoordinator()

        let outcome = await coordinator.runStop(
            quitting: false,
            timeout: 0.05,
            sendStopSignal: { }
        )

        XCTAssertTrue(outcome.isTimedOut)
    }

    func testCmdQStopTimesOutWhenStoppedNeverFires() async {
        let coordinator = LiveStopPolicyCoordinator()

        let outcome = await coordinator.runStop(
            quitting: true,
            timeout: 0.05,
            sendStopSignal: { }
        )

        XCTAssertTrue(outcome.isTimedOut)
    }

    // MARK: - Stop signal dispatch errors

    func testStopSignalFailurePreventsAwaitAndReportsError() async {
        let coordinator = LiveStopPolicyCoordinator()

        let outcome = await coordinator.runStop(
            quitting: false,
            timeout: 5.0,
            sendStopSignal: { throw StopSignalError() }
        )

        guard let error = outcome.sendSignalError as? StopSignalError else {
            return XCTFail("Expected StopSignalError, got \(outcome)")
        }
        XCTAssertEqual(error.localizedDescription, "stop signal could not dispatch")
    }

    func testStopSignalFailureReturnsBeforeTimeoutDuration() async {
        let coordinator = LiveStopPolicyCoordinator()
        let start = Date()

        _ = await coordinator.runStop(
            quitting: true,
            timeout: 3.0,
            sendStopSignal: { throw StopSignalError() }
        )

        let elapsed = Date().timeIntervalSince(start)
        XCTAssertLessThan(elapsed, 1.0, "send-signal failure must short-circuit the timeout wait")
    }

    // MARK: - Resolve cancels timeout sleep

    func testResolveBeforeTimeoutCancelsTheTimeoutSleep() async {
        let coordinator = LiveStopPolicyCoordinator()
        let start = Date()

        Task { @MainActor in
            try? await Task.sleep(nanoseconds: 30_000_000)
            coordinator.resolveFinalized(true)
        }

        let outcome = await coordinator.runStop(
            quitting: false,
            timeout: 5.0,
            sendStopSignal: { }
        )

        XCTAssertTrue(outcome.isCompleted)
        // Should resolve in ~30ms, far less than the 5s timeout.
        XCTAssertLessThan(Date().timeIntervalSince(start), 1.0)
    }

    // MARK: - Cmd+Q countdown observer

    func testCmdQOnTickQuitProgressFiresWhileWaiting() async {
        let coordinator = LiveStopPolicyCoordinator()
        var ticks: [Int] = []

        Task { @MainActor in
            // Resolve after ~2.2s so two countdown ticks fire (3-1=2 and 2-1=1).
            try? await Task.sleep(nanoseconds: 2_200_000_000)
            coordinator.resolveStopped(true)
        }

        let outcome = await coordinator.runStop(
            quitting: true,
            timeout: 3.0,
            sendStopSignal: { },
            onTickQuitProgress: { remaining in
                ticks.append(remaining)
            }
        )

        XCTAssertTrue(outcome.isCompleted)
        XCTAssertFalse(ticks.isEmpty, "expected at least one countdown tick before resolve")
    }

    // MARK: - cancelAll drains pending continuations

    func testCancelAllDrainsPendingContinuationsAsFailure() async {
        let coordinator = LiveStopPolicyCoordinator()

        Task { @MainActor in
            try? await Task.sleep(nanoseconds: 30_000_000)
            coordinator.cancelAll()
        }

        let outcome = await coordinator.runStop(
            quitting: false,
            timeout: 60.0,
            sendStopSignal: { }
        )

        // cancelAll resolves the awaiting continuation with `false`, which the
        // coordinator translates into `.timedOut`.
        XCTAssertTrue(outcome.isTimedOut)
    }

    /// Concurrent two-queue scenario: an in-app Stop and a Cmd+Q-style Stop
    /// are both in flight (one on `awaitingFinalized`, the other on
    /// `awaitingStopped`). `cancelAll` must drain both queues — neither
    /// caller should hang on a continuation the coordinator dropped.
    func testCancelAllDrainsFinalizedAndStoppedQueuesSimultaneously() async {
        let coordinator = LiveStopPolicyCoordinator()

        // Launch two concurrent stops on the two different queues.
        async let inAppOutcome = coordinator.runStop(
            quitting: false,
            timeout: 60.0,
            sendStopSignal: { }
        )
        async let quitOutcome = coordinator.runStop(
            quitting: true,
            timeout: 60.0,
            sendStopSignal: { }
        )

        Task { @MainActor in
            // Give both runStop tasks a moment to register their continuations
            // on the respective queues, then cancel both.
            try? await Task.sleep(nanoseconds: 40_000_000)
            coordinator.cancelAll()
        }

        let outcomes = await (inAppOutcome, quitOutcome)
        XCTAssertTrue(outcomes.0.isTimedOut, "awaitingFinalized continuation must be drained")
        XCTAssertTrue(outcomes.1.isTimedOut, "awaitingStopped continuation must be drained")
    }
}
