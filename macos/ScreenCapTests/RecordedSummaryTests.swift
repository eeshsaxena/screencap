import XCTest
@testable import ScreenCap

/// U2 — pins the pure `RecordedSummaryBuilder` fold: the meaningful-only filter,
/// carry-forward per-app attribution, the protected-interval skip, and the honest
/// empty state. These are the digest's load-bearing invariants (KTD1/KTD4); the
/// pane (U3) and wiring (U4) build on top.
final class RecordedSummaryTests: XCTestCase {

    /// Epoch-seconds base for synthetic events; offsets keep the arithmetic
    /// readable. `blocked_intervals` / `protected_intervals` are epoch-MS.
    private let base = 1_700_000_000.0

    /// Build one event at `base + offset` seconds. Window events carry the app
    /// name in `content.appName` (the only events that do — mirrors the parser).
    private func event(
        _ type: String, at offset: Double, app: String? = nil
    ) -> TimelineEvent {
        var content = TimelineEventContent()
        if let app { content.appName = .value(app) }
        return TimelineEvent(
            relativeSeconds: offset,
            absoluteTimestamp: base + offset,
            type: type,
            category: TimelineEvent.Category.from(eventType: type),
            content: content
        )
    }

    /// An epoch-MS interval spanning `[base + startS, base + endS]`.
    private func interval(_ startS: Double, _ endS: Double) -> CapturedInterval {
        CapturedInterval(
            startMs: Int((base + startS) * 1000),
            endMs: Int((base + endS) * 1000)
        )
    }

    private func count(_ summary: RecordedSummary, _ kind: MeaningfulEventKind) -> Int {
        summary.kindCounts.first { $0.kind == kind }?.count ?? 0
    }

    // Covers AE2: raw input is excluded; only meaningful kinds are counted, and
    // `mouse.singleclick` counts as a click (`mouse.click` is never emitted).
    func testMeaningfulOnlyFilterExcludesRawInput() {
        let events = [
            event("window.switch", at: 0, app: "Safari"),
            event("mouse.move", at: 1),          // raw — excluded
            event("mouse.singleclick", at: 2),   // meaningful — a click
            event("key.down", at: 3),            // raw — excluded
            event("key.type", at: 4),            // meaningful — typed text
            event("mouse.scroll", at: 5),        // raw — excluded
            event("network.request", at: 6),     // meaningful
        ]

        let summary = RecordedSummaryBuilder.build(
            events: events, blockedIntervals: [], protectedIntervals: []
        )

        XCTAssertEqual(count(summary, .clicks), 1, "singleclick counts as a click")
        XCTAssertEqual(count(summary, .typedText), 1)
        XCTAssertEqual(count(summary, .networkRequests), 1)
        XCTAssertEqual(count(summary, .windowSwitches), 1, "the switch itself is meaningful")
        // Raw kinds contribute nothing: total is the 4 meaningful events only.
        XCTAssertEqual(summary.totalMeaningfulCount, 4)
        XCTAssertFalse(summary.isEmpty)
    }

    // Carry-forward attribution: each meaningful event is credited to the app of
    // the nearest preceding window switch; pre-first-switch events go to unknown.
    func testCarryForwardAttributionAcrossSwitches() {
        let events = [
            event("mouse.singleclick", at: 0),                 // before any switch → unknown
            event("window.switch", at: 1, app: "Safari"),
            event("mouse.singleclick", at: 2),                 // Safari
            event("key.type", at: 3),                          // Safari
            event("window.switch", at: 4, app: "Mail"),
            event("mouse.singleclick", at: 5),                 // Mail
        ]

        let summary = RecordedSummaryBuilder.build(
            events: events, blockedIntervals: [], protectedIntervals: []
        )

        let safari = summary.apps.first { $0.appName == "Safari" }
        let mail = summary.apps.first { $0.appName == "Mail" }
        let unknown = summary.apps.first { $0.isUnknown }

        XCTAssertEqual(safari?.totalCount, 3, "switch + click + type all credited to Safari")
        XCTAssertEqual(mail?.totalCount, 2, "switch + click credited to Mail")
        XCTAssertEqual(unknown?.totalCount, 1, "the pre-first-switch click")
        XCTAssertEqual(unknown?.appName, "Unknown app")

        // Ordering: descending total, unknown pinned last regardless of count.
        XCTAssertEqual(summary.apps.map(\.appName), ["Safari", "Mail", "Unknown app"])
        XCTAssertTrue(summary.apps.last?.isUnknown == true)

        // Drill-down carries the underlying events for the app group (R6/AE4).
        XCTAssertEqual(safari?.events.count, 3)
    }

    // Covers AE3: an event inside a protected interval is dropped from both the
    // counts and its app group; the blocked line reflects the narrower
    // excluded-only `blocked_intervals`, not the protected set.
    func testProtectedIntervalSkipAndExcludedOnlyBlockedLine() {
        let events = [
            event("window.switch", at: 0, app: "Safari"),
            event("mouse.singleclick", at: 2),   // kept
            event("key.type", at: 10),           // inside the protected interval → dropped
            event("mouse.singleclick", at: 20),  // kept
        ]
        // Protected spans [9, 11]s (drops the key.type at 10). Blocked (excluded-
        // only) is a distinct, narrower window used for the reassurance line.
        let protectedIntervals = [interval(9, 11)]
        let blockedIntervals = [interval(30, 33)]

        let summary = RecordedSummaryBuilder.build(
            events: events,
            blockedIntervals: blockedIntervals,
            protectedIntervals: protectedIntervals
        )

        XCTAssertEqual(count(summary, .typedText), 0, "the typed event inside the protected span is dropped")
        XCTAssertEqual(count(summary, .clicks), 2)
        let safari = summary.apps.first { $0.appName == "Safari" }
        XCTAssertEqual(safari?.events.contains { $0.type == "key.type" }, false,
                       "dropped event is absent from the app group too")

        // Blocked line reads the excluded-only set (3s span), never the protected one.
        XCTAssertEqual(summary.blockedCount, 1)
        XCTAssertEqual(summary.blockedTotalMs, 3000)
        XCTAssertTrue(summary.hasBlocked)
    }

    // Covers AE5: zero meaningful events after the skip → isEmpty, even when the
    // recording still has blocked intervals.
    func testEmptyWhenNoMeaningfulEventsSurvive() {
        let events = [
            event("mouse.move", at: 0),
            event("key.down", at: 1),
            event("mouse.singleclick", at: 5),   // dropped by the protected span
        ]
        let summary = RecordedSummaryBuilder.build(
            events: events,
            blockedIntervals: [interval(0, 6)],
            protectedIntervals: [interval(4, 6)]
        )

        XCTAssertTrue(summary.isEmpty, "no meaningful events survive")
        XCTAssertEqual(summary.totalMeaningfulCount, 0)
        XCTAssertTrue(summary.apps.isEmpty)
        // A meaningful-empty recording can still surface its blocked reassurance.
        XCTAssertTrue(summary.hasBlocked)
    }

    func testEmptyInputYieldsEmptySummary() {
        let summary = RecordedSummaryBuilder.build(
            events: [], blockedIntervals: [], protectedIntervals: []
        )
        XCTAssertTrue(summary.isEmpty)
        XCTAssertFalse(summary.hasBlocked)
        XCTAssertEqual(summary, .empty)
    }

    // U3 display logic (KTD7): parsing-in-flight shows the loading placeholder,
    // never the empty state — even when the (partial) summary is empty.
    func testDisplayModeDistinguishesLoadingFromEmpty() {
        XCTAssertEqual(
            RecordedSummaryDisplay.mode(summary: .empty, isParsing: true), .loading)
        XCTAssertEqual(
            RecordedSummaryDisplay.mode(summary: .empty, isParsing: false), .empty)

        let nonEmpty = RecordedSummaryBuilder.build(
            events: [event("window.switch", at: 0, app: "Safari"),
                     event("mouse.singleclick", at: 1)],
            blockedIntervals: [], protectedIntervals: []
        )
        XCTAssertEqual(
            RecordedSummaryDisplay.mode(summary: nonEmpty, isParsing: false), .digest)
        // Still loading takes precedence over a ready digest.
        XCTAssertEqual(
            RecordedSummaryDisplay.mode(summary: nonEmpty, isParsing: true), .loading)
    }

    func testBlockedLineFormatting() {
        XCTAssertEqual(RecordedSummaryDisplay.blockedCountLabel(1), "1 blocked interval")
        XCTAssertEqual(RecordedSummaryDisplay.blockedCountLabel(3), "3 blocked intervals")
        XCTAssertEqual(RecordedSummaryDisplay.spanLabel(ms: 5_000), "5s")
        XCTAssertEqual(RecordedSummaryDisplay.spanLabel(ms: 65_000), "1m 5s")
        XCTAssertEqual(
            RecordedSummaryDisplay.blockedLine(count: 2, spanMs: 65_000),
            "2 blocked intervals · 1m 5s not captured")
    }
}
