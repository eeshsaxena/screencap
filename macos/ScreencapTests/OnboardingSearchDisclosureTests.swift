import XCTest
@testable import Screencap

/// Search U8 (R6): the on-by-default disclosure's decision + copy + actions, driven
/// with an injected CLI seam (no real `screencap` process).
@MainActor
final class OnboardingSearchDisclosureTests: XCTestCase {

    // MARK: - Presentation decision

    func testPresentsOnlyWhenNeitherAcknowledgedNorDeclined() {
        XCTAssertTrue(SearchDisclosurePolicy.shouldPresent(acknowledged: false, declined: false))
        XCTAssertFalse(SearchDisclosurePolicy.shouldPresent(acknowledged: true, declined: false))
        XCTAssertFalse(SearchDisclosurePolicy.shouldPresent(acknowledged: false, declined: true))
    }

    // MARK: - Honesty-gated copy (R6 mechanism points + R1 caveat)

    func testCopyIsHonestAboutTheMechanism() {
        let points = SearchDisclosureCopy.points(retentionDays: 30).joined(separator: " ").lowercased()
        XCTAssertTrue(points.contains("encrypted"))
        XCTAssertTrue(points.contains("nothing about search is uploaded") || points.contains("nothing"))
        XCTAssertTrue(points.contains("password") || points.contains("secret"))
        XCTAssertTrue(points.contains("30 days"))
        XCTAssertTrue(points.contains("pause"))
        XCTAssertTrue(points.contains("exclude"))
        XCTAssertTrue(points.contains("turn search off") || points.contains("off"))
        XCTAssertTrue(SearchDisclosureCopy.limitNote.lowercased().contains("can't catch everything"))
    }

    // MARK: - Actions run the right CLI + settle the phase

    func testEnableRunsSearchEnable() async {
        var captured: [[String]] = []
        let controller = SearchDisclosureController(run: { captured.append($0) })
        await controller.enable()
        XCTAssertEqual(captured, [["search", "enable"]])
        XCTAssertEqual(controller.phase, .enabled)
    }

    func testDeclineWritesConsentDeclined() async {
        var captured: [[String]] = []
        let controller = SearchDisclosureController(run: { captured.append($0) })
        await controller.decline()
        XCTAssertEqual(captured, [["settings", "--set", "content_index_consent_declined=true"]])
        XCTAssertEqual(controller.phase, .declined)
    }

    func testEnableFailureSurfacesError() async {
        struct Boom: Error {}
        let controller = SearchDisclosureController(run: { _ in throw Boom() })
        await controller.enable()
        if case .failed = controller.phase {} else {
            XCTFail("expected .failed, got \(controller.phase)")
        }
    }
}
