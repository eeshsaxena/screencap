import XCTest
@testable import ScreenCap

/// SCR-183 — pure VoiceOver-label builders for the Search surface. Behavioral
/// accessibility (focus, key events, announcement *posting*, Dynamic Type
/// layout) has no unit-test seam in this project and is covered by the manual
/// checklist; these tests pin the spoken *strings*.
final class SearchAccessibilityTests: XCTestCase {

    // MARK: - U1: coverage chip labels

    func testCoverageChipSpellsOutResultCount() {
        XCTAssertEqual(
            SearchAccessibility.coverageChipLabel(stream: "On screen", state: .ok(count: 3)),
            "On screen: 3 results"
        )
    }

    func testCoverageChipUsesSingularForOneResult() {
        XCTAssertEqual(
            SearchAccessibility.coverageChipLabel(stream: "Audio", state: .ok(count: 1)),
            "Audio: 1 result"
        )
    }

    func testCoverageChipNotIndexed() {
        XCTAssertEqual(
            SearchAccessibility.coverageChipLabel(stream: "On screen", state: .notIndexed),
            "On screen: not indexed"
        )
    }

    func testCoverageChipEmptyReadsAsNoMatches() {
        XCTAssertEqual(
            SearchAccessibility.coverageChipLabel(stream: "Activity", state: .empty),
            "Activity: no matches"
        )
    }

    func testCoverageChipDegradedAndUnavailable() {
        XCTAssertEqual(
            SearchAccessibility.coverageChipLabel(stream: "On screen", state: .degraded),
            "On screen: limited"
        )
        XCTAssertEqual(
            SearchAccessibility.coverageChipLabel(stream: "On screen", state: .unavailable),
            "On screen: unavailable"
        )
    }

    func testCoverageChipNotRunProducesNoLabel() {
        // `.notRun` renders no chip element, so there is nothing to announce.
        XCTAssertNil(SearchAccessibility.coverageChipLabel(stream: "Audio", state: .notRun))
    }

    func testCoverageChipLabelIsIndependentOfColor() {
        // `.empty` and `.notRun` share the same secondary color, but must read
        // differently — one is a spoken "no matches", the other is silent.
        let empty = SearchAccessibility.coverageChipLabel(stream: "Audio", state: .empty)
        let notRun = SearchAccessibility.coverageChipLabel(stream: "Audio", state: .notRun)
        XCTAssertEqual(empty, "Audio: no matches")
        XCTAssertNil(notRun)
        XCTAssertNotEqual(empty, notRun ?? "")
    }

    // MARK: - U2: result row labels

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
        XCTAssertTrue(label.contains("Salesforce"), label)
        XCTAssertTrue(label.contains("Refunds"), label)
        XCTAssertTrue(label.contains(it.timeLabel), label)            // timezone-agnostic
        XCTAssertFalse(label.contains("Approximate"), label)
    }

    func testResultRowLabelScreenIncludesSnippetAndStream() {
        let it = item(stream: .screen, snippet: "refund error")
        let label = SearchAccessibility.resultRowLabel(it)
        XCTAssertTrue(label.contains("refund error"), label)
        XCTAssertTrue(label.contains("On screen"), label)
        XCTAssertTrue(label.contains(it.timeLabel), label)
        XCTAssertFalse(label.contains("Approximate"), label)
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

    // MARK: - U6: search-outcome announcements

    private func loaded(_ count: Int) -> SearchViewModel.Phase {
        let items = Array(repeating: item(stream: .screen, snippet: "x"), count: count)
        return .loaded(SearchResults(
            items: items,
            coverage: CoverageReport(screen: .notRun, audio: .notRun, activity: .notRun),
            consentNeeded: false, timeWindow: nil, appFilter: nil
        ))
    }

    func testAnnouncementCountPlural() {
        XCTAssertEqual(SearchAccessibility.searchOutcomeAnnouncement(for: loaded(12)), "12 results")
    }

    func testAnnouncementCountSingular() {
        XCTAssertEqual(SearchAccessibility.searchOutcomeAnnouncement(for: loaded(1)), "1 result")
    }

    func testAnnouncementEmptyReadsNoMatches() {
        XCTAssertEqual(SearchAccessibility.searchOutcomeAnnouncement(for: loaded(0)), "No matches")
    }

    func testAnnouncementDaemonDown() {
        XCTAssertEqual(
            SearchAccessibility.searchOutcomeAnnouncement(for: .daemonDown),
            "ScreenCap isn\u{2019}t running"
        )
    }

    func testAnnouncementSuppressedForIdleAndSearching() {
        // No outcome to announce yet — must stay silent, not speak an empty string.
        XCTAssertNil(SearchAccessibility.searchOutcomeAnnouncement(for: .idle))
        XCTAssertNil(SearchAccessibility.searchOutcomeAnnouncement(for: .searching))
    }
}
