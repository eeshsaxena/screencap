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
        XCTAssertEqual(RecordedSummaryDisplay.blockedCountLabel(1), "1 interval")
        XCTAssertEqual(RecordedSummaryDisplay.blockedCountLabel(3), "3 intervals")
        XCTAssertEqual(RecordedSummaryDisplay.spanLabel(ms: 5_000), "5s")
        XCTAssertEqual(RecordedSummaryDisplay.spanLabel(ms: 65_000), "1m 5s")
        XCTAssertEqual(
            RecordedSummaryDisplay.blockedLine(count: 2, spanMs: 65_000),
            "Blocked at capture · 2 intervals · 1m 5s not captured")
    }

    // U6 (R9): the pane's blocked-range VoiceOver label IS the strip's blocked
    // wording — one vocabulary, one string home (DayStripAccessibility), no
    // "nothing captured" pairing.
    func testBlockedRangeAccessibilityMatchesDayStripWording() {
        let iv = interval(0, 60)
        let label = RecordedSummaryDisplay.rangeAccessibilityLabel(iv)
        XCTAssertEqual(
            label,
            DayStripAccessibility.blockedLabel(
                DayStripBlockedBand(startMs: iv.startMs, endMs: iv.endMs)),
            "pane and strip share the exact blocked sentence")
        XCTAssertTrue(label.hasPrefix("Blocked at capture, "))
        XCTAssertFalse(label.lowercased().contains("nothing captured"))
    }

    // Privacy regression (KTD3/KTD5): a window.switch to a masked/excluded app that
    // falls inside a protected interval must NOT become the carry-forward app, so
    // its name never labels a later non-protected event's group. Also pins the
    // half-open boundary: the next (allowed) switch landing exactly on the
    // protected interval's endMs is adopted, not frozen on the masked app.
    func testMaskedAppNameNeverLeaksIntoDigest() {
        let events = [
            event("window.switch", at: 0, app: "Safari"),        // allowed
            event("mouse.singleclick", at: 1),                   // Safari
            event("window.switch", at: 10, app: "1Password"),    // masked; inside protected span
            event("mouse.singleclick", at: 12),                  // masked; dropped
            event("window.switch", at: 20, app: "Mail"),         // allowed; at protected end boundary
            event("mouse.singleclick", at: 22),                  // Mail
        ]
        // Protect the masked app's whole frontmost span [10, 20); its end IS the
        // next (allowed) switch's timestamp — the boundary the fold must not snag on.
        let summary = RecordedSummaryBuilder.build(
            events: events, blockedIntervals: [], protectedIntervals: [interval(10, 20)]
        )

        XCTAssertNil(summary.apps.first { $0.appName == "1Password" },
                     "a masked app inside a protected span must never appear as a group")
        XCTAssertEqual(summary.apps.first { $0.appName == "Mail" }?.totalCount, 2,
                       "boundary switch adopted (half-open) → Mail's click credited to Mail")
        XCTAssertEqual(summary.apps.map(\.appName).sorted(), ["Mail", "Safari"])
    }

    // Half-open [start, end): an event exactly at startMs is dropped; one exactly
    // at endMs belongs to the next span and is kept.
    func testProtectedIntervalBoundaryIsHalfOpen() {
        let events = [
            event("window.switch", at: 0, app: "Safari"),
            event("mouse.singleclick", at: 10),  // == startMs of [10,20) -> dropped
            event("mouse.singleclick", at: 20),  // == endMs of [10,20) -> kept
        ]
        let summary = RecordedSummaryBuilder.build(
            events: events, blockedIntervals: [], protectedIntervals: [interval(10, 20)]
        )
        XCTAssertEqual(count(summary, .clicks), 1, "start boundary dropped, end boundary kept")
    }

    // Keyboard shortcuts (key.shortcut/key.special) count under .shortcuts;
    // window.state both advances attribution and counts as a switch (KTD4).
    func testShortcutsAndWindowStateKinds() {
        let events = [
            event("window.state", at: 0, app: "Xcode"),
            event("key.shortcut", at: 1),
            event("key.special", at: 2),
        ]
        let summary = RecordedSummaryBuilder.build(
            events: events, blockedIntervals: [], protectedIntervals: []
        )
        XCTAssertEqual(count(summary, .shortcuts), 2)
        XCTAssertEqual(count(summary, .windowSwitches), 1)
        XCTAssertEqual(summary.apps.first?.appName, "Xcode")
        XCTAssertEqual(summary.apps.first?.totalCount, 3)
    }

    // A window event with an absent/redacted app name leaves the prior app in
    // force (defensive carry-forward fallback) — no spurious unknown bucket.
    func testWindowEventWithoutNameKeepsPriorApp() {
        let events = [
            event("window.switch", at: 0, app: "Safari"),
            event("mouse.singleclick", at: 1),   // Safari
            event("window.state", at: 5),        // no app name -> currentApp stays Safari
            event("mouse.singleclick", at: 6),   // still Safari
        ]
        let summary = RecordedSummaryBuilder.build(
            events: events, blockedIntervals: [], protectedIntervals: []
        )
        XCTAssertEqual(summary.apps.first { $0.appName == "Safari" }?.totalCount, 4,
                       "switch + unnamed window.state + 2 clicks all credited to Safari")
        XCTAssertNil(summary.apps.first { $0.isUnknown }, "prior app carried; no unknown bucket")
    }

    // The unknown bucket is pinned last even when its count exceeds every named app.
    func testUnknownBucketPinnedLastEvenWhenLargest() {
        let events = [
            event("mouse.singleclick", at: 0),   // unknown
            event("mouse.singleclick", at: 1),   // unknown
            event("mouse.singleclick", at: 2),   // unknown
            event("window.switch", at: 3, app: "Safari"),
            event("mouse.singleclick", at: 4),   // Safari
        ]
        let summary = RecordedSummaryBuilder.build(
            events: events, blockedIntervals: [], protectedIntervals: []
        )
        XCTAssertEqual(summary.apps.map(\.isUnknown), [false, true],
                       "named app first, unknown pinned last despite its higher count")
        XCTAssertEqual(summary.apps.last?.totalCount, 3)
    }
}
