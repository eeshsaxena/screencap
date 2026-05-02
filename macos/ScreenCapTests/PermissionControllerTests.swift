import XCTest
@testable import ScreenCap

final class PermissionControllerTests: XCTestCase {
    func testRelaunchHelperExitsOnTimeoutInsteadOfOpeningNewInstance() {
        let script = PermissionController.relaunchHelperShellScript(
            maxPollCount: 3,
            pollIntervalSeconds: 0.1
        )

        XCTAssertTrue(script.contains("[ $i -ge 3 ] && exit 0"))
        XCTAssertFalse(script.contains("[ $i -ge 3 ] && break"))
    }

    func testRelaunchHelperExecsAppBinaryInsteadOfOpen() {
        let script = PermissionController.relaunchHelperShellScript(
            maxPollCount: 3,
            pollIntervalSeconds: 0.1
        )

        XCTAssertTrue(script.contains("exec \"$2\""))
        XCTAssertFalse(script.contains("/usr/bin/open"))
    }
}
