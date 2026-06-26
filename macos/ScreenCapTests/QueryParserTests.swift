import Foundation
import XCTest
@testable import ScreenCap

/// SCR-174 U3 — table-driven tests for the local query parser. Uses a fixed
/// UTC calendar and an injected `now` so time-expression resolution is
/// deterministic; expected windows are computed independently (not by re-running
/// the parser's phrase logic).
final class QueryParserTests: XCTestCase {
    private var cal: Calendar = {
        var c = Calendar(identifier: .gregorian)
        c.timeZone = TimeZone(secondsFromGMT: 0)!
        c.firstWeekday = 1
        return c
    }()

    private lazy var parser = QueryParser(calendar: cal)
    // 2026-06-24 15:30 UTC.
    private lazy var now: Date = cal.date(
        from: DateComponents(year: 2026, month: 6, day: 24, hour: 15, minute: 30)
    )!

    private func ms(_ d: Date) -> Int { Int((d.timeIntervalSince1970 * 1000).rounded()) }

    func testYesterdayAfternoonWithKnownApp() {
        let parsed = parser.parse("salesforce errors yesterday afternoon", now: now)

        XCTAssertEqual(parsed.appFilter, "salesforce")
        XCTAssertEqual(parsed.freeText, "errors")

        let yStart = cal.startOfDay(for: cal.date(byAdding: .day, value: -1, to: now)!)
        let expStart = cal.date(byAdding: .hour, value: 12, to: yStart)!
        let expEnd = cal.date(byAdding: .hour, value: 18, to: yStart)!
        XCTAssertEqual(parsed.timeWindow, TimeWindow(startMs: ms(expStart), endMs: ms(expEnd)))
    }

    func testPureFreeTextHasNoWindowOrApp() {
        let parsed = parser.parse("refund macro", now: now)
        XCTAssertNil(parsed.timeWindow)
        XCTAssertNil(parsed.appFilter)
        XCTAssertEqual(parsed.freeText, "refund macro")
    }

    func testTodayResolvesToFullDay() {
        let parsed = parser.parse("what did i do today", now: now)
        XCTAssertNil(parsed.appFilter)
        XCTAssertEqual(parsed.freeText, "what did i do")

        let start = cal.startOfDay(for: now)
        let end = cal.date(byAdding: .day, value: 1, to: start)!
        XCTAssertEqual(parsed.timeWindow, TimeWindow(startMs: ms(start), endMs: ms(end)))
    }

    func testLastWeekdayResolvesToMostRecentPastOccurrence() {
        let parsed = parser.parse("errors last tuesday", now: now)
        XCTAssertEqual(parsed.freeText, "errors")
        XCTAssertNil(parsed.appFilter)

        // Independent reference: walk back from yesterday to the nearest Tuesday.
        var d = cal.date(byAdding: .day, value: -1, to: cal.startOfDay(for: now))!
        while cal.component(.weekday, from: d) != 3 {  // 3 == Tuesday
            d = cal.date(byAdding: .day, value: -1, to: d)!
        }
        let end = cal.date(byAdding: .day, value: 1, to: d)!
        XCTAssertEqual(parsed.timeWindow, TimeWindow(startMs: ms(d), endMs: ms(end)))
    }

    func testAppWithPartOfDay() {
        let parsed = parser.parse("slack this morning", now: now)
        XCTAssertEqual(parsed.appFilter, "slack")
        XCTAssertEqual(parsed.freeText, "")

        let start0 = cal.startOfDay(for: now)
        let expStart = cal.date(byAdding: .hour, value: 6, to: start0)!
        let expEnd = cal.date(byAdding: .hour, value: 12, to: start0)!
        XCTAssertEqual(parsed.timeWindow, TimeWindow(startMs: ms(expStart), endMs: ms(expEnd)))
    }

    func testUnrecognizedAppStaysInFreeText() {
        let parsed = parser.parse("frobnicator dashboard", now: now)
        XCTAssertNil(parsed.appFilter)
        XCTAssertNil(parsed.timeWindow)
        XCTAssertEqual(parsed.freeText, "frobnicator dashboard")
    }

    func testEmptyAndWhitespaceQueryParseToEmpty() {
        XCTAssertTrue(parser.parse("", now: now).isEmpty)
        XCTAssertTrue(parser.parse("    ", now: now).isEmpty)
    }
}
