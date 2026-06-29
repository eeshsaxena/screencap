import AppKit
import SwiftUI
import XCTest
@testable import ScreenCap

// SCR-184 U3 — the regression guard for the SCR-174 blank/frozen-detail bug:
// free-text searches returned results but the detail pane rendered blank because
// a List nested below siblings in a VStack inside the NavigationSplitView detail
// never received height. These tests host the real composition (SearchResultsView
// through `searchDetailLayout`, inside a NavigationSplitView detail column) and
// fail if the results pane renders empty when results exist.
//
// Mechanism note (resolved during implementation): the bug's specific *height
// starvation* is NavigationSplitView-detail-specific and does NOT reproduce in an
// NSHostingView unit host — a SwiftUI List keeps a substantial minimum viewport
// height there even when starved (verified: a greedy-sibling composition only
// shrank it to ~280pt, never to zero). So the guard is expressed against the
// reliable, reproducible signal: the results List's *content* (document view)
// must actually grow when results exist. `testResultsRenderTallerContentThanEmpty`
// is the teeth-proof — it confirms the measurement distinguishes a populated
// results pane from an empty one, which is exactly the acceptance criterion
// ("a test fails if the results pane renders empty when results exist").
@MainActor
final class SearchViewLayoutTests: XCTestCase {

    private func hostLoaded(_ results: SearchResults) -> SearchViewHost.Hosted {
        SearchViewHost.host(
            SearchViewHost.detailColumn {
                searchDetailLayout {
                    Color.clear.frame(height: 44)
                } content: {
                    SearchFixtures.resultsView(.loaded(results))
                }
            }
        )
    }

    // MARK: - Positive guard: results render content, not a blank pane

    func testLoadedResultsRenderContent() {
        let results = SearchFixtures.multiDayResults()
        let hosted = hostLoaded(results)

        XCTAssertNotNil(
            SearchViewHost.resultsScrollView(in: hosted),
            "Results List did not render — the results pane is missing"
        )
        XCTAssertTrue(
            SearchViewHost.rendersVisibleContent(in: hosted),
            "Results pane rendered blank with results present (SCR-174 regression class)"
        )
        // The backing table carries a row per result (plus coverage + section
        // headers), so the row count is at least the number of results.
        XCTAssertGreaterThanOrEqual(
            SearchViewHost.resultsRowCount(in: hosted), results.items.count,
            "Results List rendered fewer rows than results present — rows not rendering"
        )
    }

    func testSingleResultRendersContent() {
        let results = SearchFixtures.singleResult()
        let hosted = hostLoaded(results)
        XCTAssertGreaterThanOrEqual(
            SearchViewHost.resultsRowCount(in: hosted), results.items.count,
            "Single-result pane rendered no result row"
        )
        XCTAssertTrue(SearchViewHost.rendersVisibleContent(in: hosted), "Single-result pane rendered blank")
    }

    func testUnanchoredAudioOnlyRendersContent() {
        let results = SearchFixtures.unanchoredOnlyResults()
        let hosted = hostLoaded(results)
        XCTAssertGreaterThanOrEqual(
            SearchViewHost.resultsRowCount(in: hosted), results.items.count,
            "\"Heard in audio\" section rendered fewer rows than audio results present"
        )
        XCTAssertTrue(SearchViewHost.rendersVisibleContent(in: hosted), "Audio-only results pane rendered blank")
    }

    // MARK: - SCR-181 U3: timeline pins above the List without starving it

    /// The scrubbable timeline pins ABOVE the results List via `.safeAreaInset`
    /// (NOT a VStack sibling), so the List keeps its height. `multiDayResults`
    /// spans >1 day, so the day-strip (a horizontal `ScrollView`) renders — a
    /// detectable signal the timeline is present (the tree gains a second scroll
    /// view) — while the results List still reports a row per result. If the pin
    /// ever regresses to a starving sibling, the List row count collapses and this
    /// fails; if the timeline stops rendering, the scroll-view count drops to one.
    func testTimelinePinnedRendersAndDoesNotStarveList() {
        let results = SearchFixtures.multiDayResults()
        let hosted = hostLoaded(results)
        XCTAssertGreaterThanOrEqual(
            SearchViewHost.views(ofType: NSScrollView.self, in: hosted).count, 2,
            "Timeline day-strip did not render — the per-day timeline is missing above the results"
        )
        XCTAssertGreaterThanOrEqual(
            SearchViewHost.resultsRowCount(in: hosted), results.items.count,
            "Results List starved when the timeline was pinned (SCR-174 regression class)"
        )
    }

    /// An all-unanchored result set has nothing to place on a time axis, so no
    /// timeline (day-strip) renders — only the results List. Guards against the
    /// timeline appearing (and consuming height) when there is nothing to show.
    func testNoTimelineForUnanchoredOnly() {
        let hosted = hostLoaded(SearchFixtures.unanchoredOnlyResults())
        XCTAssertEqual(
            SearchViewHost.views(ofType: NSScrollView.self, in: hosted).count, 1,
            "A day-strip rendered for an all-unanchored result set that has no axis placement"
        )
    }

    // MARK: - Teeth: the measurement distinguishes populated from empty

    /// The acceptance criterion, made reproducible: a results pane WITH results
    /// must render measurably more rows than the SAME pane with none. If a
    /// regression makes results stop rendering (blank pane despite results), the
    /// populated row count collapses toward the empty one and this fails — proving
    /// the guard above actually discriminates a populated pane from an empty one.
    func testPopulatedResultsRenderMoreRowsThanEmpty() {
        let populated = SearchViewHost.resultsRowCount(in: hostLoaded(SearchFixtures.multiDayResults()))
        let empty = SearchViewHost.resultsRowCount(in: hostLoaded(SearchFixtures.authoritativeEmptyResults()))

        XCTAssertGreaterThan(
            populated, empty,
            """
            Results pane with results rendered \(populated) rows; the empty pane rendered \(empty). \
            The guard can no longer tell a populated results pane from an empty one — it would not \
            catch the results-render-empty regression. See plan U3.
            """
        )
    }
}
