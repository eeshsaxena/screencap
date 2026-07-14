import XCTest
@testable import ScreenCap

/// SCR-258 U10 (KTD-16): the store-scoped Unlock gating. Unlock runs the
/// present-user gate FIRST and only calls the unlock verb on success — a cancelled
/// or failed auth stays sealed with NO verb call (fail-closed). Lock and init have
/// no gate. Driven with a fake presence evaluator + counting action closures so no
/// real biometrics or sockets are involved.
@MainActor
final class StoreControllerTests: XCTestCase {

    private final class FakePresence: PresenceEvaluating {
        var isAvailable: Bool
        var willSucceed: Bool
        init(available: Bool = true, succeeds: Bool = true) {
            self.isAvailable = available
            self.willSucceed = succeeds
        }
        func evaluatePresence(reason: String) async -> Bool { willSucceed }
    }

    /// Thread-safe call counter (the action closures are `@Sendable`).
    private final class Counter: @unchecked Sendable {
        private let lock = NSLock()
        private var value = 0
        func bump() { lock.lock(); value += 1; lock.unlock() }
        var count: Int { lock.lock(); defer { lock.unlock() }; return value }
    }

    private func makeController(
        presenceSucceeds: Bool = true,
        presenceAvailable: Bool = true,
        unlock: Counter,
        lock: Counter
    ) -> StoreController {
        StoreController(
            presenceGate: PresenceGate(
                evaluator: FakePresence(available: presenceAvailable, succeeds: presenceSucceeds),
                graceWindow: 0
            ),
            lockAction: { lock.bump() },
            unlockAction: { unlock.bump() },
            initAction: {}
        )
    }

    func testUnlockCallsVerbAfterSuccessfulAuth() async {
        let unlock = Counter(), lock = Counter()
        let controller = makeController(presenceSucceeds: true, unlock: unlock, lock: lock)
        await controller.performUnlock()
        XCTAssertEqual(unlock.count, 1, "auth passed → unlock verb called exactly once")
        XCTAssertEqual(controller.phase, .idle)
    }

    func testUnlockDoesNotCallVerbWhenAuthFails() async {
        let unlock = Counter(), lock = Counter()
        let controller = makeController(presenceSucceeds: false, unlock: unlock, lock: lock)
        await controller.performUnlock()
        XCTAssertEqual(unlock.count, 0, "auth failed → NO unlock verb call (fail-closed)")
        XCTAssertEqual(controller.phase, .idle)
    }

    func testUnlockFailsClosedWhenNoAuthSurface() async {
        let unlock = Counter(), lock = Counter()
        let controller = makeController(
            presenceSucceeds: true, presenceAvailable: false, unlock: unlock, lock: lock)
        await controller.performUnlock()
        XCTAssertEqual(unlock.count, 0, "no LA surface → fail-closed, no verb call")
    }

    func testLockCallsVerbWithoutAuth() async {
        let unlock = Counter(), lock = Counter()
        let controller = makeController(unlock: unlock, lock: lock)
        await controller.performLock()
        XCTAssertEqual(lock.count, 1)
        XCTAssertEqual(controller.phase, .idle)
    }

    func testLockSurfacesActionError() async {
        struct Boom: Error {}
        let controller = StoreController(
            presenceGate: PresenceGate(evaluator: FakePresence(), graceWindow: 0),
            lockAction: { throw Boom() },
            unlockAction: {},
            initAction: {}
        )
        await controller.performLock()
        XCTAssertNotNil(controller.lastError, "a failed lock verb surfaces an error")
        XCTAssertEqual(controller.phase, .idle)
    }
}
