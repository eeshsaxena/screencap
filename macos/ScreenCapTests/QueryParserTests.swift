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

    // MARK: - SCR-179 U3: broadened time expressions

    /// Build a [start-of-`startDay`, start-of-`endDayExclusive`) window from
    /// explicit y/m/d components — an independent reference, not the parser's logic.
    private func dayRange(
        _ s: (Int, Int, Int), _ e: (Int, Int, Int)
    ) -> TimeWindow {
        let start = cal.date(from: DateComponents(year: s.0, month: s.1, day: s.2))!
        let end = cal.date(from: DateComponents(year: e.0, month: e.1, day: e.2))!
        return TimeWindow(startMs: ms(start), endMs: ms(end))
    }

    func testThisMonthResolvesToCalendarMonth() {
        let parsed = parser.parse("invoices this month", now: now)
        XCTAssertEqual(parsed.freeText, "invoices")
        XCTAssertEqual(parsed.timeWindow, dayRange((2026, 6, 1), (2026, 7, 1)))
    }

    func testLastMonthResolvesToPreviousCalendarMonth() {
        let parsed = parser.parse("standup last month", now: now)
        XCTAssertEqual(parsed.timeWindow, dayRange((2026, 5, 1), (2026, 6, 1)))
    }

    func testExplicitDateResolvesToThatFullDay() {
        let parsed = parser.parse("errors june 3", now: now)
        XCTAssertEqual(parsed.freeText, "errors")
        XCTAssertNil(parsed.appFilter)
        XCTAssertEqual(parsed.timeWindow, dayRange((2026, 6, 3), (2026, 6, 4)))
    }

    func testExplicitDateAbbreviatedMonthAndDayMonthOrder() {
        XCTAssertEqual(parser.parse("jun 3", now: now).timeWindow, dayRange((2026, 6, 3), (2026, 6, 4)))
        XCTAssertEqual(parser.parse("3 june", now: now).timeWindow, dayRange((2026, 6, 3), (2026, 6, 4)))
        XCTAssertEqual(parser.parse("june 3rd", now: now).timeWindow, dayRange((2026, 6, 3), (2026, 6, 4)))
    }

    func testExplicitDateInFutureResolvesToPreviousYear() {
        // now is 2026-06-24; "december 5" hasn't happened in 2026 yet.
        let parsed = parser.parse("notes december 5", now: now)
        XCTAssertEqual(parsed.timeWindow, dayRange((2025, 12, 5), (2025, 12, 6)))
    }

    func testNDaysAgoResolvesToThatFullDay() {
        let parsed = parser.parse("deploy 3 days ago", now: now)
        XCTAssertEqual(parsed.freeText, "deploy")
        XCTAssertEqual(parsed.timeWindow, dayRange((2026, 6, 21), (2026, 6, 22)))
    }

    func testLastNWeeksIsTrailingCompleteDaysIncludingToday() {
        // 14 trailing days ending today (2026-06-24): 2026-06-11 .. 2026-06-25.
        let parsed = parser.parse("changes last 2 weeks", now: now)
        XCTAssertEqual(parsed.freeText, "changes")
        XCTAssertEqual(parsed.timeWindow, dayRange((2026, 6, 11), (2026, 6, 25)))
    }

    func testLastNDaysIsTrailingWindow() {
        // 3 trailing days ending today: 2026-06-22 .. 2026-06-25.
        let parsed = parser.parse("last 3 days", now: now)
        XCTAssertEqual(parsed.timeWindow, dayRange((2026, 6, 22), (2026, 6, 25)))
    }

    func testStandalonePartOfDayResolvesToToday() {
        let parsed = parser.parse("standup afternoon", now: now)
        XCTAssertEqual(parsed.freeText, "standup")
        let start0 = cal.startOfDay(for: now)
        let expStart = cal.date(byAdding: .hour, value: 12, to: start0)!
        let expEnd = cal.date(byAdding: .hour, value: 18, to: start0)!
        XCTAssertEqual(parsed.timeWindow, TimeWindow(startMs: ms(expStart), endMs: ms(expEnd)))
    }

    func testExplicitDateRangeResolvesInclusiveOfBothEnds() {
        let parsed = parser.parse("incidents june 1 to june 3", now: now)
        XCTAssertEqual(parsed.freeText, "incidents")
        XCTAssertEqual(parsed.timeWindow, dayRange((2026, 6, 1), (2026, 6, 4)))
    }

    func testExplicitDateRangeSameMonthShorthand() {
        let parsed = parser.parse("june 1 to 3", now: now)
        XCTAssertEqual(parsed.timeWindow, dayRange((2026, 6, 1), (2026, 6, 4)))
    }

    func testLastZeroWeeksFallsThroughToFreeText() {
        let parsed = parser.parse("last 0 weeks", now: now)
        XCTAssertNil(parsed.timeWindow)
        XCTAssertEqual(parsed.freeText, "last 0 weeks")
    }

    func testBareMonthWithoutDayIsFreeText() {
        let parsed = parser.parse("june planning", now: now)
        XCTAssertNil(parsed.timeWindow)
        XCTAssertEqual(parsed.freeText, "june planning")
    }

    // MARK: - SCR-179 U4: broadened + normalized app/site recognition

    func testExpandedStaticAppIsRecognized() {
        let parsed = parser.parse("notes obsidian today", now: now)
        XCTAssertEqual(parsed.appFilter, "obsidian")
        XCTAssertEqual(parsed.freeText, "notes")
    }

    func testTypedDomainNormalizesToBrand() {
        // github.com → github (brand is in the recognized vocabulary).
        XCTAssertEqual(parser.parse("issues github.com", now: now).appFilter, "github")
        XCTAssertEqual(parser.parse("pr at www.github.com/org/repo", now: now).appFilter, "github")
    }

    func testUnknownDomainDoesNotMisfire() {
        // "main.py" must not become an app filter just because it has a dot.
        let parsed = parser.parse("edit main.py", now: now)
        XCTAssertNil(parsed.appFilter)
        XCTAssertEqual(parsed.freeText, "edit main.py")
    }

    func testMultiWordAppNameMatchesAsPhrase() {
        let parsed = parser.parse("crash google chrome yesterday", now: now)
        XCTAssertEqual(parsed.appFilter, "google chrome")
        XCTAssertEqual(parsed.freeText, "crash")
    }

    func testAmbiguousWordIsNotAnAppFilter() {
        let parsed = parser.parse("find the docs about pricing", now: now)
        XCTAssertNil(parsed.appFilter)
        XCTAssertEqual(parsed.freeText, "find the docs about pricing")
    }

    func testInjectedVocabularyIsRecognized() {
        // A term only present in the injected vocabulary (not the static seed)
        // is recognized; the same parser without it falls through to free text.
        let injected = QueryParser(
            calendar: cal,
            knownApps: QueryParser.defaultKnownApps + ["acmecorp"]
        )
        XCTAssertEqual(injected.parse("acmecorp dashboard", now: now).appFilter, "acmecorp")
        XCTAssertNil(parser.parse("acmecorp dashboard", now: now).appFilter)
    }

    func testInjectedVocabularyStillExcludesAmbiguousWords() {
        // Even if the index yields an app literally named "Mail", it must not
        // misfire into an app filter.
        let injected = QueryParser(
            calendar: cal,
            knownApps: QueryParser.defaultKnownApps + ["mail"]
        )
        XCTAssertNil(injected.parse("send the mail today", now: now).appFilter)
    }

    func testVocabularyTermsNormalizesIndexValues() {
        let terms = QueryParser.vocabularyTerms(
            appNames: ["Superhuman", "Mail", "Google Chrome"],
            hostnames: ["github.com", "mail.google.com", "main.py"]
        )
        XCTAssertTrue(terms.contains("superhuman"))
        XCTAssertTrue(terms.contains("google chrome"))
        XCTAssertTrue(terms.contains("github"))   // brand from github.com
        XCTAssertTrue(terms.contains("google"))   // brand from mail.google.com
        XCTAssertFalse(terms.contains("mail"))    // ambiguous word dropped
    }
}
