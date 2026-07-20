import XCTest
@testable import Screencap

/// SCR-183 — the pure VoiceOver result-row label builder, applied by the
/// Recall palette's rows (U10). Behavioral accessibility (focus, key events,
/// announcement *posting*, Dynamic Type layout) has no unit-test seam in this
/// project and is covered by the manual checklist; these tests pin the spoken
/// *strings*. The retired sidebar Search pane's chip / announcement / count
/// builders were deleted with it (U14), along with their tests.
final class SearchAccessibilityTests: XCTestCase {

    private func item(
        stream: SearchResultItem.Stream,
        anchorMs: Int? = 1_700_000_000_000,
        approximate: Bool = false,
        snippet: String? = nil,
        app: String? = nil,
        title: String? = nil
    ) -> SearchResultItem {
        SearchResultItem(
            id: "t-\(stream.rawValue)", stream: stream, recording: "rec",
            anchorMs: anchorMs, approximate: approximate, score: 0,
            snippet: snippet, app: app, title: title
        )
    }

    func testResultRowLabelActivityLeadsWithAppThenTitleThenTime() {
        let it = item(stream: .activity, app: "Salesforce", title: "Refunds")
        let label = SearchAccessibility.resultRowLabel(it)
        // Pin the full token order ("{app}. {title}. at {time}"), not just
        // presence — a `.contains` check would pass on a reordered label.
        XCTAssertEqual(label, "Salesforce. Refunds. at \(it.timeLabel)")
        XCTAssertFalse(label.contains("Approximate"), label)
    }

    func testResultRowLabelScreenIncludesSnippetAndStream() {
        let it = item(stream: .screen, snippet: "refund error")
        let label = SearchAccessibility.resultRowLabel(it)
        // Pin the full token order ("{snippet}. On screen. at {time}").
        XCTAssertEqual(label, "refund error. On screen. at \(it.timeLabel)")
        XCTAssertFalse(label.contains("Approximate"), label)
    }

    func testResultRowLabelActivityWithNilAppFallsBackToActivityWord() {
        // No captured app name → the row leads with the generic "Activity".
        let it = item(stream: .activity, app: nil, title: nil)
        let label = SearchAccessibility.resultRowLabel(it)
        XCTAssertEqual(label, "Activity. at \(it.timeLabel)")
    }

    func testResultRowLabelActivityWithNilTitleOmitsSecondary() {
        // A missing window title skips the secondary entirely — never speaks "nil".
        let it = item(stream: .activity, app: "Finder", title: nil)
        let label = SearchAccessibility.resultRowLabel(it)
        XCTAssertFalse(label.contains("nil"), label)
        XCTAssertEqual(label, "Finder. at \(it.timeLabel)")
    }

    func testResultRowLabelAudioMarksApproximate() {
        let it = item(stream: .audio, approximate: true, snippet: "discussed pricing")
        let label = SearchAccessibility.resultRowLabel(it)
        XCTAssertTrue(label.contains("discussed pricing"), label)
        XCTAssertTrue(label.contains("Heard in audio"), label)
        XCTAssertTrue(label.contains("Approximate, from audio"), label)
    }

    func testResultRowLabelMissingSnippetUsesNoPreview() {
        let it = item(stream: .screen, snippet: nil)
        let label = SearchAccessibility.resultRowLabel(it)
        XCTAssertTrue(label.contains("(no preview)"), label)
    }

    func testResultRowLabelUnanchoredReadsTimeUnknown() {
        // The visible row shows "—"; the spoken label must not read "dash".
        let it = item(stream: .audio, anchorMs: nil, snippet: "heard later")
        let label = SearchAccessibility.resultRowLabel(it)
        XCTAssertTrue(label.contains("time unknown"), label)
        XCTAssertFalse(label.contains("\u{2014}"), label)            // no em dash
    }
}
