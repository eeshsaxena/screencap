import SwiftUI
import XCTest
@testable import ScreenCap

/// U10 — the Recall palette's pure state derivation (every SearchViewState →
/// its palette variant), wrapping keyboard nav, the ↵ jump target, the
/// esc-cancellation contract, and hosted render smoke checks per variant.
@MainActor
final class RecallPaletteStateTests: XCTestCase {

    // MARK: - Variant derivation

    func testIdleVariantCarriesRecentChips() {
        let state = RecallPalette.state(
            phase: .idle, consentDeclined: false, backfillState: .hidden,
            recents: ["meeting with John"]
        )
        XCTAssertEqual(state.body, .idle(recents: ["meeting with John"]))
        XCTAssertFalse(state.showsConsentBanner)
    }

    func testSearchingAndErrorVariants() {
        XCTAssertEqual(
            RecallPalette.state(phase: .searching, consentDeclined: false,
                                backfillState: .hidden, recents: []).body,
            .searching
        )
        XCTAssertEqual(
            RecallPalette.state(phase: .daemonDown, consentDeclined: false,
                                backfillState: .hidden, recents: []).body,
            .daemonDown
        )
    }

    func testConsentNeededShowsBannerUnlessDeclinedOrBackfillActive() {
        let phase = SearchViewModel.Phase.loaded(SearchFixtures.consentNeededResults())
        let shown = RecallPalette.state(
            phase: phase, consentDeclined: false, backfillState: .hidden, recents: []
        )
        XCTAssertTrue(shown.showsConsentBanner)
        XCTAssertEqual(shown.body, .results)

        let declined = RecallPalette.state(
            phase: phase, consentDeclined: true, backfillState: .hidden, recents: []
        )
        XCTAssertFalse(declined.showsConsentBanner)

        // The backfill affordance displaces the banner (SearchResultsView rule).
        let backfilling = RecallPalette.state(
            phase: phase, consentDeclined: false, backfillState: .offering, recents: []
        )
        XCTAssertFalse(backfilling.showsConsentBanner)
        XCTAssertEqual(backfilling.backfill, .offering)
    }

    func testEmptyAndResultsVariants() {
        XCTAssertEqual(
            RecallPalette.state(
                phase: .loaded(SearchFixtures.noMatchesResults()),
                consentDeclined: false, backfillState: .hidden, recents: []
            ).body,
            .empty
        )
        XCTAssertEqual(
            RecallPalette.state(
                phase: .loaded(SearchFixtures.multiDayResults()),
                consentDeclined: false, backfillState: .hidden, recents: []
            ).body,
            .results
        )
    }

    // MARK: - Grouping

    func testGroupsByDayNewestFirstWithUnanchoredTrailing() {
        var results = SearchFixtures.multiDayResults()
        results.items.append(SearchFixtures.unanchoredAudioItem())
        let groups = RecallPalette.groups(results)
        XCTAssertEqual(groups.first?.label, "TODAY")
        XCTAssertEqual(groups.first?.items.count, 2)
        XCTAssertEqual(groups.last?.label, RecallPalette.unanchoredLabel)
        XCTAssertEqual(groups.count, 3)
    }

    // MARK: - Keyboard nav (wrap)

    func testSelectionMovesAndWraps() {
        let results = SearchFixtures.multiDayResults()
        let ordered = RecallPalette.orderedItems(results)
        XCTAssertEqual(ordered.count, 3)

        // nil selection: ↓ selects the row after the effective (first) row.
        let second = RecallPalette.next(after: ordered[0].id, in: ordered)
        XCTAssertEqual(second, ordered[1].id)
        // Wrap forward off the end.
        XCTAssertEqual(RecallPalette.next(after: ordered[2].id, in: ordered), ordered[0].id)
        // Wrap backward off the start.
        XCTAssertEqual(RecallPalette.previous(before: ordered[0].id, in: ordered), ordered[2].id)
        // A stale id (from a previous result set) re-enters at the first row.
        XCTAssertEqual(RecallPalette.next(after: "stale-id", in: ordered), ordered[0].id)
        // Empty results: no movement, no crash.
        XCTAssertNil(RecallPalette.next(after: nil, in: []))
    }

    func testEffectiveSelectionFallsBackToFirstRow() {
        let ordered = RecallPalette.orderedItems(SearchFixtures.multiDayResults())
        XCTAssertEqual(RecallPalette.effectiveSelection(nil, in: ordered)?.id, ordered.first?.id)
        XCTAssertEqual(RecallPalette.effectiveSelection(ordered[2].id, in: ordered)?.id, ordered[2].id)
        XCTAssertEqual(RecallPalette.effectiveSelection("stale", in: ordered)?.id, ordered.first?.id)
        XCTAssertNil(RecallPalette.effectiveSelection(nil, in: []))
    }

    // MARK: - ↵ jump target

    func testJumpTargetIsTheHitsDayAndTimestamp() {
        let item = SearchFixtures.item(daysAgo: 1, hour: 14)
        let target = RecallPalette.timelineTarget(for: item)
        XCTAssertNotNil(target)
        XCTAssertEqual(target?.seekMs, item.anchorMs)
        let expectedDay = Calendar.current.startOfDay(
            for: Date(timeIntervalSince1970: Double(item.anchorMs!) / 1000)
        )
        XCTAssertEqual(target?.day, expectedDay)
        XCTAssertNil(
            RecallPalette.timelineTarget(for: SearchFixtures.unanchoredAudioItem()),
            "an unanchored hit has no moment to land on"
        )
    }

    // MARK: - esc cancels the in-flight query

    func testDismissCancelsInFlightQueryTask() async {
        // Hook-counter fake: the run body parks until cancelled and records
        // what it observed.
        let hook = QueryHook()
        let runner = RecallPaletteQueryRunner(debounceNanos: 0) { _ in
            await hook.started()
            do {
                try await Task.sleep(nanoseconds: 10_000_000_000)
            } catch {
                await hook.cancelled()
            }
        }
        runner.search("meeting with John", debounced: false)
        let task = runner.searchTask
        XCTAssertNotNil(task)
        // Let the search body actually start before cancelling (bounded spin so
        // a regression fails instead of hanging the suite).
        var spins = 0
        while await hook.startCount == 0, spins < 100_000 {
            await Task.yield()
            spins += 1
        }
        runner.cancel()
        await task?.value
        let starts = await hook.startCount
        let cancels = await hook.cancelCount
        XCTAssertEqual(starts, 1)
        XCTAssertEqual(cancels, 1, "dismissal must cancel the in-flight query")
        XCTAssertNil(runner.searchTask, "the cancelled task handle is released")
    }

    /// A fresh keystroke supersedes the previous in-flight query (the existing
    /// SearchView single-handle pattern).
    func testNewSearchCancelsPreviousTask() async {
        let hook = QueryHook()
        let runner = RecallPaletteQueryRunner(debounceNanos: 0) { _ in
            await hook.started()
            do {
                try await Task.sleep(nanoseconds: 10_000_000_000)
            } catch {
                await hook.cancelled()
            }
        }
        runner.search("first", debounced: false)
        let first = runner.searchTask
        var spins = 0
        while await hook.startCount == 0, spins < 100_000 {
            await Task.yield()
            spins += 1
        }
        runner.search("second", debounced: false)
        await first?.value
        let cancels = await hook.cancelCount
        XCTAssertEqual(cancels, 1)
        runner.cancel()
    }

    private actor QueryHook {
        private(set) var startCount = 0
        private(set) var cancelCount = 0
        func started() { startCount += 1 }
        func cancelled() { cancelCount += 1 }
    }

    // MARK: - Render smoke checks (hosted)

    private func host(
        _ phase: SearchViewModel.Phase,
        consentDeclined: Bool = false,
        backfillState: SearchViewModel.BackfillUIState = .hidden,
        recents: [String] = []
    ) -> SearchViewHost.Hosted {
        SearchViewHost.host(
            RecallPaletteContent(
                phase: phase,
                consentDeclined: consentDeclined,
                backfillState: backfillState,
                recentSearches: recents,
                queryTerms: [],
                selectedResultID: nil,
                frameIndex: nil,
                thumbnailLoader: nil
            )
            .frame(width: 620)
            .background(Color.white),
            size: CGSize(width: 620, height: 520)
        )
    }

    func testEachVariantRendersVisibleContent() {
        for hosted in [
            host(.idle, recents: ["payroll"]),
            host(.searching),
            host(.daemonDown),
            host(.loaded(SearchFixtures.multiDayResults())),
            host(.loaded(SearchFixtures.noMatchesResults())),
            host(.loaded(SearchFixtures.consentNeededResults())),
            host(.loaded(SearchFixtures.consentNeededResults()), backfillState: .offering),
            host(.loaded(SearchFixtures.multiDayResults()),
                 backfillState: .indexing(done: 3, total: 9, failed: 0)),
        ] {
            XCTAssertTrue(
                SearchViewHost.rendersVisibleContent(in: hosted),
                "palette variant must not render blank"
            )
        }
    }
}
