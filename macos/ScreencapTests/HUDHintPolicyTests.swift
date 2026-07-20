import XCTest
@testable import Screencap

/// U5 — the one-time first-hide hint shows only when it has never been shown.
final class HUDHintPolicyTests: XCTestCase {
    func testShouldShowOnlyWhenNotShownBefore() {
        XCTAssertTrue(HUDHintPolicy.shouldShow(hasShownBefore: false))
        XCTAssertFalse(HUDHintPolicy.shouldShow(hasShownBefore: true))
    }
}
