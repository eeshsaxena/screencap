import XCTest
@testable import Screencap

/// Search U6 (R4): the present-user gate's grace-window + fail-closed rules, driven
/// with a fake evaluator + injected clock so no real biometrics are involved.
@MainActor
final class PresenceGateTests: XCTestCase {

    private final class FakePresence: PresenceEvaluating {
        var isAvailable: Bool
        var willSucceed: Bool
        private(set) var promptCount = 0
        init(available: Bool = true, succeeds: Bool = true) {
            self.isAvailable = available
            self.willSucceed = succeeds
        }
        func evaluatePresence(reason: String) async -> Bool {
            promptCount += 1
            return willSucceed
        }
    }

    func testFirstViewPromptsAndUnlocks() async {
        let fake = FakePresence(succeeds: true)
        let gate = PresenceGate(evaluator: fake, graceWindow: 900, now: { Date(timeIntervalSince1970: 0) })
        XCTAssertFalse(gate.isUnlocked)
        let ok = await gate.ensurePresent()
        XCTAssertTrue(ok)
        XCTAssertEqual(fake.promptCount, 1)
        XCTAssertTrue(gate.isUnlocked)
    }

    func testSecondViewWithinGraceDoesNotReprompt() async {
        let fake = FakePresence(succeeds: true)
        var t = Date(timeIntervalSince1970: 0)
        let gate = PresenceGate(evaluator: fake, graceWindow: 900, now: { t })
        _ = await gate.ensurePresent()
        t = Date(timeIntervalSince1970: 300)  // 5 min later, within 15-min grace
        let ok = await gate.ensurePresent()
        XCTAssertTrue(ok)
        XCTAssertEqual(fake.promptCount, 1)  // no second prompt
    }

    func testExpiryReprompts() async {
        let fake = FakePresence(succeeds: true)
        var t = Date(timeIntervalSince1970: 0)
        let gate = PresenceGate(evaluator: fake, graceWindow: 900, now: { t })
        _ = await gate.ensurePresent()
        t = Date(timeIntervalSince1970: 1000)  // past the 900s window
        XCTAssertFalse(gate.isUnlocked)
        _ = await gate.ensurePresent()
        XCTAssertEqual(fake.promptCount, 2)
    }

    func testAuthFailureKeepsContentLocked() async {
        let fake = FakePresence(succeeds: false)
        let gate = PresenceGate(evaluator: fake, now: { Date(timeIntervalSince1970: 0) })
        let ok = await gate.ensurePresent()
        XCTAssertFalse(ok)
        XCTAssertFalse(gate.isUnlocked)
    }

    func testNoAuthHardwareFailsClosed() async {
        let fake = FakePresence(available: false, succeeds: true)
        let gate = PresenceGate(evaluator: fake, now: { Date(timeIntervalSince1970: 0) })
        let ok = await gate.ensurePresent()
        XCTAssertFalse(ok)
        XCTAssertEqual(fake.promptCount, 0)  // never even prompted
    }

    func testLockDropsGrace() async {
        let fake = FakePresence(succeeds: true)
        let gate = PresenceGate(evaluator: fake, now: { Date(timeIntervalSince1970: 0) })
        _ = await gate.ensurePresent()
        XCTAssertTrue(gate.isUnlocked)
        gate.lock()
        XCTAssertFalse(gate.isUnlocked)
    }
}
