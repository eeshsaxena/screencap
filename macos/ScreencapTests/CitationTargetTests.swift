import XCTest
@testable import Screencap

/// U12 (R13, R14, KTD-1/KTD-11, AE4) — the pure citation/day-time presentation
/// models. Pins that a `(recording, timestamp_ms)` pointer renders as day + time
/// (never a recording name), that an unanchored citation keeps its day, and that
/// the Review window title is derived from a date, never the `rec-<timestamp>`
/// directory name.
final class CitationTargetTests: XCTestCase {

    /// A GMT calendar so HH:mm and day boundaries are deterministic under any
    /// host timezone.
    private var gmt: Calendar {
        var c = Calendar(identifier: .gregorian)
        c.timeZone = TimeZone(identifier: "GMT")!
        return c
    }

    /// An absolute instant (and its ms) at the given GMT wall-clock time.
    private func instant(
        _ year: Int, _ month: Int, _ day: Int, _ hour: Int, _ minute: Int
    ) -> Date {
        var comps = DateComponents()
        comps.year = year; comps.month = month; comps.day = day
        comps.hour = hour; comps.minute = minute
        return gmt.date(from: comps)!
    }

    // MARK: - CitationTarget day + time

    func testDayTimeLabelIsTodayPlusClock() {
        let now = instant(2026, 7, 18, 20, 0)
        let anchor = instant(2026, 7, 18, 14, 32)
        let label = CitationTarget.dayTimeLabel(
            anchorMs: Int(anchor.timeIntervalSince1970 * 1000), now: now, calendar: gmt
        )
        XCTAssertEqual(label, "Today · 14:32")
    }

    func testDayTimeLabelYesterday() {
        let now = instant(2026, 7, 18, 9, 0)
        let anchor = instant(2026, 7, 17, 14, 32)
        let label = CitationTarget.dayTimeLabel(
            anchorMs: Int(anchor.timeIntervalSince1970 * 1000), now: now, calendar: gmt
        )
        XCTAssertEqual(label, "Yesterday · 14:32")
    }

    func testDayTimeLabelOlderDayUsesDayVocabulary() {
        let now = instant(2026, 7, 18, 9, 0)
        let anchor = instant(2026, 7, 1, 8, 5)
        let label = CitationTarget.dayTimeLabel(
            anchorMs: Int(anchor.timeIntervalSince1970 * 1000), now: now, calendar: gmt
        )
        // Reuses DaysModel.label ("Wednesday, 1 July") — the single day vocab.
        XCTAssertTrue(label.hasSuffix(" · 08:05"), "clock half preserved: \(label)")
        XCTAssertTrue(label.contains("July"), "day half uses the day vocabulary: \(label)")
        XCTAssertFalse(label.contains("rec-"), "never a recording name")
    }

    func testDayOnlyLabelKeepsTheDay() {
        let now = instant(2026, 7, 18, 9, 0)
        let day = gmt.startOfDay(for: instant(2026, 7, 18, 3, 0))
        let label = CitationTarget.dayOnlyLabel(day: day, now: now, calendar: gmt)
        XCTAssertEqual(label, "Today · time unknown")
    }

    func testDayForAnchorMsMatchesStartOfDay() {
        let anchor = instant(2026, 7, 18, 23, 59)
        let day = CitationTarget.day(
            forAnchorMs: Int(anchor.timeIntervalSince1970 * 1000), calendar: gmt
        )
        XCTAssertEqual(day, gmt.startOfDay(for: anchor))
    }

    // MARK: - CitationTarget hit title (palette)

    func testHitTitlePrefersTitleThenApp() {
        XCTAssertEqual(
            CitationTarget.hitTitle(title: "Docs — Safari", app: "Safari", anchorMs: 1, recordingDay: nil),
            "Docs — Safari"
        )
        XCTAssertEqual(
            CitationTarget.hitTitle(title: nil, app: "Safari", anchorMs: 1, recordingDay: nil),
            "Safari"
        )
        XCTAssertEqual(
            CitationTarget.hitTitle(title: "", app: "Safari", anchorMs: 1, recordingDay: nil),
            "Safari", "an empty title is skipped, not rendered"
        )
    }

    func testHitTitleFallsBackToMomentThenDayNeverRecording() {
        let now = instant(2026, 7, 18, 20, 0)
        let anchor = instant(2026, 7, 18, 14, 32)
        let anchorMs = Int(anchor.timeIntervalSince1970 * 1000)
        // Anchored, no title/app → the moment.
        XCTAssertEqual(
            CitationTarget.hitTitle(
                title: nil, app: nil, anchorMs: anchorMs, recordingDay: nil, now: now, calendar: gmt
            ),
            "Today · 14:32"
        )
        // Unanchored, recording day known → the day, "time unknown".
        let day = gmt.startOfDay(for: anchor)
        XCTAssertEqual(
            CitationTarget.hitTitle(
                title: nil, app: nil, anchorMs: nil, recordingDay: day, now: now, calendar: gmt
            ),
            "Today · time unknown"
        )
        // Unanchored, no known day → honest "Time unknown", never a rec name.
        XCTAssertEqual(
            CitationTarget.hitTitle(title: nil, app: nil, anchorMs: nil, recordingDay: nil),
            "Time unknown"
        )
    }

    // MARK: - ReviewTitle (R14 / AE4)

    func testReviewTitleFromStartedAt() {
        let now = instant(2026, 7, 18, 20, 0)
        let started = instant(2026, 7, 18, 13, 53)
        let title = ReviewTitle.title(
            startedAt: started.timeIntervalSince1970,
            recordingName: "rec-20260718T135300",
            now: now, calendar: gmt
        )
        XCTAssertEqual(title, "Review · Today · 13:53")
        XCTAssertFalse(title.contains("rec-"), "never the recording name")
    }

    func testReviewTitleParsesCanonicalRecName() {
        let now = instant(2026, 7, 18, 20, 0)
        // No startedAt → parse the rec-%Y%m%dT%H%M%S directory name.
        let title = ReviewTitle.title(
            startedAt: nil, recordingName: "rec-20260718T135300", now: now, calendar: gmt
        )
        XCTAssertEqual(title, "Review · Today · 13:53")
    }

    func testReviewTitleTreatsZeroStartedAtAsUnknown() {
        // A null-timing sentinel (0) is not a real instant → parse the name.
        let now = instant(2026, 7, 18, 20, 0)
        let title = ReviewTitle.title(
            startedAt: 0, recordingName: "rec-20260718T135300", now: now, calendar: gmt
        )
        XCTAssertEqual(title, "Review · Today · 13:53")
    }

    func testReviewTitleFallsBackToBareReviewNeverLeaksName() {
        // A legacy / custom name that doesn't parse and no startedAt → bare
        // "Review", NEVER the raw name (AE4).
        for name in ["rec-2026-06-26-001", "my custom name", "rec-not-a-date"] {
            let title = ReviewTitle.title(startedAt: nil, recordingName: name, calendar: gmt)
            XCTAssertEqual(title, "Review", "unparseable name → bare Review: \(name)")
            XCTAssertFalse(title.contains(name), "the recording name never leaks: \(name)")
        }
    }
}
