import XCTest
@testable import ScreenCap

/// SCR-183 U4 — the pure "which result does Return open" resolution. The arrow
/// navigation and key handling themselves are AppKit behaviors with no unit-test
/// seam (covered by the manual macOS-13 checklist); this pins the selection→item
/// mapping that the Return handler relies on.
final class SearchKeyboardNavTests: XCTestCase {

    private func item(_ id: String) -> SearchResultItem {
        SearchResultItem(
            id: id, stream: .screen, recording: "rec",
            anchorMs: 1_700_000_000_000, approximate: false, score: 0,
            snippet: "hit", app: nil, title: nil
        )
    }

    private func results(_ ids: [String]) -> SearchResults {
        SearchResults(
            items: ids.map(item),
            coverage: CoverageReport(screen: .ok(count: ids.count), audio: .notRun, activity: .notRun),
            consentNeeded: false, timeWindow: nil, appFilter: nil
        )
    }

    func testResolvesSelectedItem() {
        let r = results(["a", "b", "c"])
        XCTAssertEqual(searchReviewTarget(for: "b", in: r)?.id, "b")
    }

    func testNilSelectionResolvesToNil() {
        // Return with no selection must be a no-op, not "open row 0".
        let r = results(["a", "b", "c"])
        XCTAssertNil(searchReviewTarget(for: nil, in: r))
    }

    func testStaleSelectionResolvesToNil() {
        // Selection left over after a re-search points at an id no longer present.
        let r = results(["a", "b"])
        XCTAssertNil(searchReviewTarget(for: "gone", in: r))
    }

    func testEmptyResultsResolvesToNil() {
        XCTAssertNil(searchReviewTarget(for: "a", in: results([])))
    }
}
