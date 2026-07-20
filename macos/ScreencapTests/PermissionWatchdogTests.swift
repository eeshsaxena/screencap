import AppKit
import XCTest
@testable import Screencap

@MainActor
final class PermissionWatchdogTests: XCTestCase {
    func testStartInvokesCheckClosureOnTimer() async {
        let watchdog = LivePermissionWatchdog(interval: 0.05)
        let invoked = expectation(description: "check invoked at least once")
        invoked.assertForOverFulfill = false

        var count = 0
        watchdog.start {
            count += 1
            invoked.fulfill()
        }

        await fulfillment(of: [invoked], timeout: 1.0)
        watchdog.stop()
        XCTAssertGreaterThan(count, 0)
    }

    func testStopPreventsFurtherTimerInvocations() async throws {
        let watchdog = LivePermissionWatchdog(interval: 0.05)
        var count = 0
        watchdog.start { count += 1 }
        try await Task.sleep(nanoseconds: 120_000_000) // ~2 ticks
        let countAtStop = count
        watchdog.stop()
        try await Task.sleep(nanoseconds: 200_000_000) // a few more would-be ticks
        XCTAssertEqual(count, countAtStop, "watchdog continued firing after stop()")
    }

    func testRestartInvalidatesPreviousTimer() async throws {
        let watchdog = LivePermissionWatchdog(interval: 0.05)
        var firstCount = 0
        var secondCount = 0

        watchdog.start { firstCount += 1 }
        try await Task.sleep(nanoseconds: 80_000_000)
        watchdog.start { secondCount += 1 }
        let firstFreezePoint = firstCount
        try await Task.sleep(nanoseconds: 200_000_000)
        watchdog.stop()

        // The first closure must not continue firing after the second start().
        XCTAssertEqual(firstCount, firstFreezePoint)
        XCTAssertGreaterThan(secondCount, 0)
    }

    func testActivationNotificationTriggersCheckClosure() async {
        let watchdog = LivePermissionWatchdog(interval: 60.0) // long, so only the observer fires
        let invoked = expectation(description: "check invoked from activation notification")
        invoked.assertForOverFulfill = false

        watchdog.start {
            invoked.fulfill()
        }

        // Synthesize the workspace activation notification the live observer
        // listens for. NotificationCenter delivers asynchronously on the main
        // queue, hence the fulfillment wait.
        NSWorkspace.shared.notificationCenter.post(
            name: NSWorkspace.didActivateApplicationNotification,
            object: nil
        )

        await fulfillment(of: [invoked], timeout: 0.5)
        watchdog.stop()
    }
}
