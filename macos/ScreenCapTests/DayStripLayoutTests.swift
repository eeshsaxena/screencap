import XCTest
@testable import ScreenCap

/// U9 — the day strip's pure geometry: dynamic axis bounds (outward hour
/// rounding, 8h minimum, day clamping), time→x mapping, marker clustering, the
/// R7 blocked-band mapping, and the accessibility label builders.
final class DayStripLayoutTests: XCTestCase {

    private let hour = DayStripLayout.hourMs
    /// An arbitrary local-midnight origin; the layout only does arithmetic on it.
    private let dayStart = 1_800_000_000_000

    // MARK: - Axis bounds

    func testAxisBoundsRoundOutwardToTheHour() {
        let bounds = DayStripLayout.axisBounds(dayStartMs: dayStart, spans: [
            (startMs: dayStart + 9 * hour + 20 * 60_000, endMs: dayStart + 17 * hour + 40 * 60_000),
        ])
        XCTAssertEqual(bounds.startMs, dayStart + 9 * hour, "start floors to the hour")
        XCTAssertEqual(bounds.endMs, dayStart + 18 * hour, "end ceils to the hour")
    }

    func testAxisBoundsEnforceEightHourMinimum() {
        let bounds = DayStripLayout.axisBounds(dayStartMs: dayStart, spans: [
            (startMs: dayStart + 10 * hour, endMs: dayStart + 11 * hour),
        ])
        XCTAssertEqual(bounds.spanMs, DayStripLayout.minSpanMs)
        XCTAssertEqual(bounds.startMs, dayStart + 10 * hour, "minimum extends forward from the union")
    }

    func testAxisBoundsMinimumPullsBackFromDayEnd() {
        // A late-evening recording: extending forward would cross midnight.
        let bounds = DayStripLayout.axisBounds(dayStartMs: dayStart, spans: [
            (startMs: dayStart + 22 * hour, endMs: dayStart + 23 * hour),
        ])
        XCTAssertEqual(bounds.endMs, dayStart + 24 * hour)
        XCTAssertEqual(bounds.startMs, dayStart + 16 * hour)
    }

    /// A midnight-spanning recording arrives day-clamped from the daemon, but a
    /// client-side span leaking past the day must still clamp into it.
    func testAxisBoundsClampIntoTheDay() {
        let bounds = DayStripLayout.axisBounds(dayStartMs: dayStart, spans: [
            (startMs: dayStart - 2 * hour, endMs: dayStart + 25 * hour),
        ])
        XCTAssertEqual(bounds.startMs, dayStart)
        XCTAssertEqual(bounds.endMs, dayStart + 24 * hour)
    }

    func testAxisBoundsDefaultToWorkingWindowWhenSpanless() {
        let bounds = DayStripLayout.axisBounds(dayStartMs: dayStart, spans: [])
        XCTAssertEqual(bounds.startMs, dayStart + 8 * hour)
        XCTAssertEqual(bounds.endMs, dayStart + 16 * hour)
    }

    // MARK: - Time → x

    func testTimeToXMapsLinearlyAndClamps() {
        let bounds = DayStripLayout.Bounds(startMs: dayStart, endMs: dayStart + 10 * hour)
        XCTAssertEqual(DayStripLayout.x(forMs: dayStart, bounds: bounds, width: 500), 0)
        XCTAssertEqual(DayStripLayout.x(forMs: dayStart + 5 * hour, bounds: bounds, width: 500), 250)
        XCTAssertEqual(DayStripLayout.x(forMs: dayStart + 10 * hour, bounds: bounds, width: 500), 500)
        XCTAssertEqual(DayStripLayout.x(forMs: dayStart - hour, bounds: bounds, width: 500), 0, "clamps below")
        XCTAssertEqual(DayStripLayout.x(forMs: dayStart + 11 * hour, bounds: bounds, width: 500), 500, "clamps above")
        XCTAssertEqual(DayStripLayout.x(forMs: dayStart, bounds: bounds, width: 0), 0, "zero width → no NaN")
    }

    func testXToMsRoundTrips() {
        let bounds = DayStripLayout.Bounds(startMs: dayStart, endMs: dayStart + 8 * hour)
        let ms = dayStart + 3 * hour
        let x = DayStripLayout.x(forMs: ms, bounds: bounds, width: 640)
        XCTAssertEqual(DayStripLayout.ms(forX: x, bounds: bounds, width: 640), ms)
        XCTAssertEqual(DayStripLayout.ms(forX: -10, bounds: bounds, width: 640), bounds.startMs, "clamps below")
        XCTAssertEqual(DayStripLayout.ms(forX: 900, bounds: bounds, width: 640), bounds.endMs, "clamps above")
    }

    func testHourTicksStepWidensPastTwelveHours() {
        let short = DayStripLayout.Bounds(startMs: dayStart, endMs: dayStart + 8 * hour)
        XCTAssertEqual(DayStripLayout.hourTicks(bounds: short).count, 5, "8h span → 2h steps")
        let full = DayStripLayout.Bounds(startMs: dayStart, endMs: dayStart + 24 * hour)
        XCTAssertEqual(DayStripLayout.hourTicks(bounds: full).count, 7, "24h span → 4h steps")
    }

    // MARK: - Marker clustering

    /// Same single-linkage contract as the retired SearchTimelineLayout's
    /// cluster: chained neighbors within the threshold collapse to their mean x.
    func testMarkerClusteringChainsWithinThreshold() {
        XCTAssertEqual(DayStripLayout.clusterXs([10, 20, 30, 200], thresholdPx: 14), [20, 200])
        XCTAssertEqual(DayStripLayout.clusterXs([5], thresholdPx: 14), [5])
        XCTAssertEqual(DayStripLayout.clusterXs([], thresholdPx: 14), [])
        XCTAssertEqual(
            DayStripLayout.clusterXs([30, 10], thresholdPx: 0), [10, 30],
            "non-positive threshold disables clustering but still sorts"
        )
    }

    // MARK: - R7: blocked bands

    /// Hatching may claim "blocked" only for `blocked_proven` intervals —
    /// `unverifiable` intervals and plain gaps never produce a band.
    func testBlockedBandsComeOnlyFromProvenIntervals() {
        let spans = [
            DaySegmentRecording(
                name: "rec-a", startMs: dayStart, endMs: dayStart + hour,
                blockedProven: [DayBlockedInterval(startMs: dayStart + 100, endMs: dayStart + 200)],
                unverifiable: [DayBlockedInterval(startMs: dayStart + 300, endMs: dayStart + 400)]
            ),
            DaySegmentRecording(
                name: "rec-b", startMs: dayStart + 2 * hour, endMs: dayStart + 3 * hour,
                unverifiable: [DayBlockedInterval(startMs: dayStart + 2 * hour, endMs: dayStart + 3 * hour)]
            ),
        ]
        XCTAssertEqual(
            DayStripBlockedBand.provenBands(from: spans),
            [DayStripBlockedBand(startMs: dayStart + 100, endMs: dayStart + 200)]
        )
    }

    // MARK: - Accessibility labels

    func testAccessibilityLabelsNameSegmentBlockedAndPlayhead() {
        let segment = DayStripSegment(
            recording: "rec-a", title: "Payroll walkthrough",
            startMs: dayStart, endMs: dayStart + hour
        )
        XCTAssertTrue(DayStripAccessibility.segmentLabel(segment).hasPrefix("Payroll walkthrough, "))
        let band = DayStripBlockedBand(startMs: dayStart, endMs: dayStart + hour)
        XCTAssertTrue(DayStripAccessibility.blockedLabel(band).hasPrefix("Blocked, nothing captured"))
        XCTAssertTrue(DayStripAccessibility.playheadLabel(ms: dayStart).hasPrefix("Playhead at "))
    }
}
