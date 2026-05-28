import XCTest
@testable import ScreenCap

/// U3 tests — pin the `ReviewWindowOpener` behavior so a future SDK regression
/// or refactor of the bridge surface is caught early. The actual SwiftUI
/// `WindowGroup` materialization is not driven from XCTest; the opener
/// closure is the seam we can observe.
@MainActor
final class ReviewWindowOpenerTests: XCTestCase {
    override func tearDown() {
        ReviewWindowOpener.shared.openReview = nil
        super.tearDown()
    }

    func testOpenForwardsRecordingNameToRegisteredClosure() {
        var receivedNames: [String] = []
        ReviewWindowOpener.shared.openReview = { receivedNames.append($0) }

        let opened = ReviewWindowOpener.shared.open(recordingName: "rec-2026-05-28-001")

        XCTAssertTrue(opened)
        XCTAssertEqual(receivedNames, ["rec-2026-05-28-001"])
    }

    func testOpeningDistinctNamesInvokesClosureOncePerCall() {
        var receivedNames: [String] = []
        ReviewWindowOpener.shared.openReview = { receivedNames.append($0) }

        ReviewWindowOpener.shared.open(recordingName: "rec-A")
        ReviewWindowOpener.shared.open(recordingName: "rec-B")

        XCTAssertEqual(receivedNames, ["rec-A", "rec-B"])
    }

    /// The plan calls out the WindowGroup-may-dedupe-by-value-on-macOS-14+
    /// behavior. The opener layer itself does NOT dedupe — repeat calls
    /// dispatch repeat invocations into SwiftUI, and SwiftUI decides
    /// per-platform whether to focus the existing window or create a new
    /// one. Pinning this lets us notice if someone later adds dedup at
    /// this layer (which would silently regress the multi-window contract
    /// from R3).
    func testRepeatedOpenForSameNameDispatchesEachCall() {
        var callCount = 0
        ReviewWindowOpener.shared.openReview = { _ in callCount += 1 }

        ReviewWindowOpener.shared.open(recordingName: "rec-X")
        ReviewWindowOpener.shared.open(recordingName: "rec-X")
        ReviewWindowOpener.shared.open(recordingName: "rec-X")

        XCTAssertEqual(callCount, 3)
    }

    func testOpenWithoutRegisteredClosureIsSafeNoOpAndReportsFailure() {
        ReviewWindowOpener.shared.openReview = nil

        let opened = ReviewWindowOpener.shared.open(recordingName: "rec-not-ready")

        // No crash, and the return value flags the "not ready" state so a
        // future caller (e.g. a CLI-triggered open before the main window
        // ever materialized) can decide whether to surface an error or queue.
        XCTAssertFalse(opened)
    }

    func testReassigningClosureReplacesPriorRegistration() {
        var firstCalls = 0
        var secondCalls = 0
        ReviewWindowOpener.shared.openReview = { _ in firstCalls += 1 }
        ReviewWindowOpener.shared.openReview = { _ in secondCalls += 1 }

        ReviewWindowOpener.shared.open(recordingName: "rec-Y")

        XCTAssertEqual(firstCalls, 0)
        XCTAssertEqual(secondCalls, 1)
    }

    /// Ensures the published scene id and the opener-bridge id agree.
    /// Both production callsites read `ReviewWindowID`, so this is mostly a
    /// pin against accidental rename — but a typo on either side would
    /// silently break the row → window dispatch.
    func testReviewWindowIDConstantIsStable() {
        XCTAssertEqual(ReviewWindowID, "review")
    }
}
