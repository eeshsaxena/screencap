import XCTest
@testable import ScreenCap

@MainActor
final class WindowOpenerTests: XCTestCase {
    override func tearDown() {
        WindowOpener.shared.openMain = nil
        super.tearDown()
    }

    func testInvokingRegisteredClosureRunsItExactlyOnce() {
        var callCount = 0
        WindowOpener.shared.openMain = { callCount += 1 }

        WindowOpener.shared.openMain?()

        XCTAssertEqual(callCount, 1)
    }

    func testInvokingWhenNoClosureRegisteredIsSafeNoOp() {
        WindowOpener.shared.openMain = nil

        WindowOpener.shared.openMain?()
        // No crash, no assertion to fire — reaching this line is the test.
    }

    func testReassigningClosureReplacesPriorRegistration() {
        var firstCalls = 0
        var secondCalls = 0
        WindowOpener.shared.openMain = { firstCalls += 1 }
        WindowOpener.shared.openMain = { secondCalls += 1 }

        WindowOpener.shared.openMain?()

        XCTAssertEqual(firstCalls, 0)
        XCTAssertEqual(secondCalls, 1)
    }
}
