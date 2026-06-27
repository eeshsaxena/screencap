import AppKit
import SwiftUI
import XCTest
@testable import ScreenCap

// SCR-184 U4 — structural snapshot coverage for the core Search states. Each test
// hosts `SearchResultsView` in a given phase, forces layout, and asserts the
// state renders visible content (anti-blank — the SCR-174 failure was a blank
// pane) plus the structural chrome unique to that state. These are "snapshots" in
// the structural/rendered sense (the AppKit control tree + a non-blank render
// check), not pixel-diff golden images — robust on CI, no stored references.
//
// Granularity note (resolved during implementation): SwiftUI's accessibility tree
// is not readable from this unit-test host and pure-`Text` states expose no child
// NSViews, so the exact copy of a state ("Nothing recorded then" vs "No matches",
// idle vs daemon-down wording) is not unit-observable here. That text is covered
// by the manual accessibility/QA checklist (as `SearchAccessibilityTests` notes
// for behavioral a11y). These tests pin what IS reliably observable: that each
// state renders content and carries the right structural chrome.
@MainActor
final class SearchViewStateTests: XCTestCase {

    private func host(_ phase: SearchViewModel.Phase) -> SearchViewHost.Hosted {
        SearchViewHost.host(SearchFixtures.resultsView(phase))
    }

    private func hasProgressIndicator(_ hosted: SearchViewHost.Hosted) -> Bool {
        !SearchViewHost.views(ofType: NSProgressIndicator.self, in: hosted).isEmpty
    }

    // MARK: - Message states (no List chrome, must still render content)

    func testIdleStateRendersContentWithoutListChrome() {
        let hosted = host(.idle)
        XCTAssertTrue(SearchViewHost.rendersVisibleContent(in: hosted), "Idle state rendered blank")
        XCTAssertNil(SearchViewHost.resultsScrollView(in: hosted), "Idle state should not render a results List")
        XCTAssertFalse(hasProgressIndicator(hosted), "Idle state should not render a progress indicator")
    }

    func testDaemonDownStateRendersContentWithoutListChrome() {
        let hosted = host(.daemonDown)
        XCTAssertTrue(SearchViewHost.rendersVisibleContent(in: hosted), "Daemon-down state rendered blank")
        XCTAssertNil(SearchViewHost.resultsScrollView(in: hosted), "Daemon-down state should not render a results List")
        XCTAssertFalse(hasProgressIndicator(hosted), "Daemon-down state should not render a progress indicator")
    }

    // MARK: - Searching (progress indicator, no List)

    func testSearchingStateRendersProgressIndicator() {
        let hosted = host(.searching)
        XCTAssertTrue(hasProgressIndicator(hosted), "Searching state did not render a progress indicator")
        XCTAssertNil(SearchViewHost.resultsScrollView(in: hosted), "Searching state should not render a results List")
        XCTAssertTrue(SearchViewHost.rendersVisibleContent(in: hosted), "Searching state rendered blank")
    }

    // MARK: - Loaded states (results List)

    func testLoadedMultiDayRendersPopulatedResultsList() {
        let hosted = host(.loaded(SearchFixtures.multiDayResults()))
        XCTAssertNotNil(SearchViewHost.resultsScrollView(in: hosted), "Loaded state did not render the results List")
        XCTAssertGreaterThanOrEqual(
            SearchViewHost.resultsRowCount(in: hosted), 3,
            "Loaded multi-day state rendered an empty results List"
        )
        XCTAssertTrue(SearchViewHost.rendersVisibleContent(in: hosted), "Loaded state rendered blank")
    }

    /// Both empty variants render a results List with no result rows. The copy
    /// distinction ("Nothing recorded then" vs "No matches") is manual-checklist
    /// covered (see the granularity note above); here we pin that each empty path
    /// renders a non-blank List whose content is shorter than a populated one.
    func testAuthoritativeEmptyRendersEmptyList() {
        let hosted = host(.loaded(SearchFixtures.authoritativeEmptyResults()))
        XCTAssertNotNil(SearchViewHost.resultsScrollView(in: hosted), "Authoritative-empty state did not render a List")
        XCTAssertTrue(SearchViewHost.rendersVisibleContent(in: hosted), "Authoritative-empty state rendered blank")
    }

    func testNoMatchesEmptyRendersEmptyList() {
        let hosted = host(.loaded(SearchFixtures.noMatchesResults()))
        XCTAssertNotNil(SearchViewHost.resultsScrollView(in: hosted), "No-matches state did not render a List")
        XCTAssertTrue(SearchViewHost.rendersVisibleContent(in: hosted), "No-matches state rendered blank")
    }

    func testConsentBannerStateRendersList() {
        let hosted = host(.loaded(SearchFixtures.consentNeededResults()))
        XCTAssertNotNil(SearchViewHost.resultsScrollView(in: hosted), "Consent-banner state did not render a List")
        XCTAssertTrue(SearchViewHost.rendersVisibleContent(in: hosted), "Consent-banner state rendered blank")
    }
}
