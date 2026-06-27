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

    func testAnnouncementAuthoritativeEmptyReadsNothingRecorded() {
        // A time-scoped query over a recorded-but-empty window must announce the
        // same outcome the visible emptyRow shows ("Nothing recorded then"), not
        // the generic "No matches".
        let phase = SearchViewModel.Phase.loaded(SearchResults(
            items: [],
            coverage: CoverageReport(screen: .notRun, audio: .notRun, activity: .empty),
            consentNeeded: false,
            timeWindow: TimeWindow(startMs: 1_700_000_000_000, endMs: 1_700_000_600_000),
            appFilter: nil
        ))
        XCTAssertEqual(
            SearchAccessibility.searchOutcomeAnnouncement(for: phase),
            "Nothing recorded then"
        )
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

    // MARK: - SCR-182 U3: result-count + truncation helpers

    private var utcCal: Calendar = {
        var c = Calendar(identifier: .gregorian)
        c.timeZone = TimeZone(secondsFromGMT: 0)!
        return c
    }()

    private func results(_ items: [SearchResultItem], truncated: Bool = false) -> SearchResults {
        SearchResults(
            items: items,
            coverage: CoverageReport(screen: .notRun, audio: .notRun, activity: .notRun),
            consentNeeded: false, timeWindow: nil, appFilter: nil, truncated: truncated
        )
    }

    func testResultCountLabelAcrossDays() {
        let d1 = 1_700_000_000_000          // 2023-11-14 22:13 UTC
        let oneDay = 86_400_000
        let r = results([
            item(stream: .activity, anchorMs: d1),
            item(stream: .screen, anchorMs: d1 + 1000),     // same UTC day
            item(stream: .screen, anchorMs: d1 + oneDay),   // next UTC day
        ])
        XCTAssertEqual(SearchAccessibility.resultCountLabel(r, calendar: utcCal), "3 results across 2 days")
    }

    func testResultCountLabelSingularResultAndDay() {
        let r = results([item(stream: .screen, anchorMs: 1_700_000_000_000)])
        XCTAssertEqual(SearchAccessibility.resultCountLabel(r, calendar: utcCal), "1 result across 1 day")
    }

    func testResultCountLabelUnanchoredOnlyOmitsDayClause() {
        let r = results([
            item(stream: .audio, anchorMs: nil),
            item(stream: .audio, anchorMs: nil),
        ])
        XCTAssertEqual(SearchAccessibility.resultCountLabel(r, calendar: utcCal), "2 results")
    }

    func testResultCountLabelEmptyIsNil() {
        XCTAssertNil(SearchAccessibility.resultCountLabel(results([]), calendar: utcCal))
    }

    func testTruncationNoteOnlyWhenTruncated() {
        XCTAssertNotNil(SearchAccessibility.truncationNote(results([item(stream: .screen)], truncated: true)))
        XCTAssertNil(SearchAccessibility.truncationNote(results([item(stream: .screen)], truncated: false)))
    }

    func testAnnouncementAppendsTruncationCue() {
        let items = Array(repeating: item(stream: .screen, snippet: "x"), count: 3)
        let ann = SearchAccessibility.searchOutcomeAnnouncement(for: .loaded(results(items, truncated: true)))
        XCTAssertNotNil(ann)
        XCTAssertTrue(ann!.hasPrefix("3 results"), ann ?? "")
        XCTAssertTrue(ann!.contains("narrow your search"), ann ?? "")
    }
}
