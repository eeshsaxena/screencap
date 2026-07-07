import XCTest
@testable import ScreenCap

/// U8 — the Journal's pure grouping rules (JournalModel) and the app-chip
/// resolver's caching / fail-silent contract (JournalAppChips). Rendering is
/// verified by build-and-run.
final class JournalGroupingTests: XCTestCase {

    // MARK: - Day grouping

    /// Today / Yesterday / older group by local start day, newest day first.
    func testGroupsAcrossDaysNewestFirst() {
        let cal = Calendar.current
        let now = Date()
        let today = cal.startOfDay(for: now)
        let days = JournalModel.days([
            rec(name: "old", startedAt: today.addingTimeInterval(-5 * 86_400 + 9 * 3_600)),
            rec(name: "today", startedAt: today.addingTimeInterval(10 * 3_600)),
            rec(name: "yesterday", startedAt: today.addingTimeInterval(-86_400 + 15 * 3_600)),
        ], now: now, calendar: cal)

        XCTAssertEqual(days.count, 3)
        XCTAssertEqual(days[0].label, "Today")
        XCTAssertEqual(days[0].items.map(\.name), ["today"])
        XCTAssertEqual(days[1].label, "Yesterday")
        XCTAssertEqual(days[2].items.map(\.name), ["old"])
        XCTAssertFalse(days[2].label.isEmpty)
    }

    /// A 23:50 recording that runs past midnight belongs to its *start* day.
    func testMidnightAdjacentRecordingGroupsByStartDay() {
        let cal = Calendar.current
        let now = Date()
        let today = cal.startOfDay(for: now)
        // Started yesterday 23:50, 20 minutes long — ends today 00:10.
        let days = JournalModel.days(
            [rec(name: "midnight", startedAt: today.addingTimeInterval(-600), durationSeconds: 1_200)],
            now: now, calendar: cal
        )
        XCTAssertEqual(days.map(\.label), ["Yesterday"])
    }

    /// Cards within a day read chronologically (morning → evening), matching
    /// the day timeline's axis.
    func testItemsWithinADayAreChronological() {
        let cal = Calendar.current
        let now = Date()
        let today = cal.startOfDay(for: now)
        let days = JournalModel.days([
            rec(name: "afternoon", startedAt: today.addingTimeInterval(14 * 3_600)),
            rec(name: "morning", startedAt: today.addingTimeInterval(9 * 3_600)),
        ], now: now, calendar: cal)
        XCTAssertEqual(days.first?.items.map(\.name), ["morning", "afternoon"])
    }

    /// A recording whose start time is underivable is parked in a trailing
    /// "Undated" section rather than hidden or given a fabricated date.
    func testUndatedRecordingsLandInTrailingSection() {
        let cal = Calendar.current
        let now = Date()
        let today = cal.startOfDay(for: now)
        let days = JournalModel.days([
            rec(name: "dated", startedAt: today.addingTimeInterval(3_600)),
            rec(name: "mystery", startedAt: nil, date: "not-a-date"),
        ], now: now, calendar: cal)
        XCTAssertEqual(days.map(\.label), ["Today", "Undated"])
        XCTAssertEqual(days.last?.items.map(\.name), ["mystery"])
    }

    // MARK: - Labels + count string

    func testOlderDayLabelsUseTheSerifHeadingVocabulary() {
        var cal = Calendar(identifier: .gregorian)
        cal.timeZone = TimeZone(identifier: "UTC")!
        let now = date(cal, 2026, 7, 3)
        XCTAssertEqual(JournalModel.label(for: date(cal, 2026, 7, 3), now: now, calendar: cal), "Today")
        XCTAssertEqual(JournalModel.label(for: date(cal, 2026, 7, 2), now: now, calendar: cal), "Yesterday")
        XCTAssertEqual(JournalModel.label(for: date(cal, 2026, 7, 1), now: now, calendar: cal), "Wednesday, 1 July")
        XCTAssertEqual(
            JournalModel.label(for: date(cal, 2025, 12, 31), now: now, calendar: cal),
            "Wednesday, 31 December 2025",
            "days outside the current year carry the year"
        )
    }

    /// The mono heading count (design 397) — "N recordings", singular-aware.
    func testCountString() {
        let single = JournalModel.Day(day: nil, label: "Today", items: [rec(name: "a", startedAt: nil)])
        XCTAssertEqual(single.countText, "1 recording")
        let triple = JournalModel.Day(
            day: nil, label: "Today",
            items: ["a", "b", "c"].map { rec(name: $0, startedAt: nil) }
        )
        XCTAssertEqual(triple.countText, "3 recordings")
    }

    // MARK: - Summary line

    /// The card summary renders only when the namer produced one — nil and
    /// blank both hide the line (U8 test scenario "summary hidden when null").
    func testSummaryLineHiddenWhenNullOrBlank() {
        XCTAssertNil(JournalModel.summaryLine(rec(name: "a", startedAt: nil, summary: nil)))
        XCTAssertNil(JournalModel.summaryLine(rec(name: "b", startedAt: nil, summary: "  \\n")))
        XCTAssertEqual(
            JournalModel.summaryLine(rec(name: "c", startedAt: nil, summary: "Running the mid-month cycle")),
            "Running the mid-month cycle"
        )
    }

    // MARK: - Dominant app

    func testDominantAppPicksMostFrequentFirstSeenOnTies() {
        let zoomTwice = [row(app: "Safari"), row(app: "Zoom"), row(app: "Zoom")]
        XCTAssertEqual(JournalModel.dominantApp(zoomTwice), "Zoom")
        let tie = [row(app: "Safari"), row(app: "Zoom"), row(app: "Zoom"), row(app: "Safari")]
        XCTAssertEqual(JournalModel.dominantApp(tie), "Safari", "first-seen wins a tie")
        XCTAssertNil(JournalModel.dominantApp([row(app: nil), row(app: "")]))
        XCTAssertNil(JournalModel.dominantApp([]))
    }

    // MARK: - App-chip resolver

    /// timeline.query failure → chip omitted (nullable contract), never an error.
    @MainActor
    func testAppChipOmittedOnTimelineQueryFailure() async {
        let service = FakeJournalSearchService()
        service.shouldThrow = true
        let chips = JournalAppChips(service: service)
        let recording = rec(name: "r1", startedAt: nil)
        await chips.resolve(recording)
        XCTAssertNil(chips.app(for: recording))
    }

    /// One query per recording, cached — including after a failure.
    @MainActor
    func testAppChipResolvesOncePerRecording() async {
        let service = FakeJournalSearchService()
        service.rows = [row(app: "Gusto"), row(app: "Gusto"), row(app: "Slack")]
        let chips = JournalAppChips(service: service)
        let recording = rec(name: "r1", startedAt: nil)
        await chips.resolve(recording)
        await chips.resolve(recording)
        XCTAssertEqual(service.timelineCalls, 1)
        XCTAssertEqual(chips.app(for: recording), "Gusto")
    }

    // MARK: - Fixtures

    private func rec(
        name: String,
        startedAt: Date?,
        durationSeconds: Double = 300,
        summary: String? = nil,
        date: String = "2026-07-03"
    ) -> RecordingSummary {
        let started = startedAt.map { String($0.timeIntervalSince1970) } ?? "null"
        let summaryJSON = summary.map { "\"\($0)\"" } ?? "null"
        let json = """
        {"name":"\(name)","date":"\(date)","duration":"0:10","size_mb":"x",
         "has_audio":false,"transcribed":false,"uploaded":false,"is_stub":false,
         "chunks_total":0,"chunks_uploaded":0,"intent":null,
         "started_at":\(started),"duration_seconds":\(durationSeconds),"drops":null,
         "size_bytes":0,"summary":\(summaryJSON)}
        """
        return try! JSONDecoder().decode(RecordingSummary.self, from: Data(json.utf8))
    }

    private func row(app: String?) -> TimelineRow {
        TimelineRow(recording: "r", timestampMs: 0, app: app, title: nil)
    }

    private func date(_ cal: Calendar, _ y: Int, _ m: Int, _ d: Int) -> Date {
        cal.date(from: DateComponents(year: y, month: m, day: d))!
    }
}

/// Search-verb fake for the app-chip resolver — only `timelineQuery` is real.
private final class FakeJournalSearchService: SearchService, @unchecked Sendable {
    var rows: [TimelineRow] = []
    var shouldThrow = false
    private(set) var timelineCalls = 0

    struct Failure: Error {}

    func timelineQuery(_ req: TimelineQueryRequest) async throws -> TimelineQueryResponse {
        timelineCalls += 1
        if shouldThrow { throw Failure() }
        return TimelineQueryResponse(rows: rows, coverage: .authoritative)
    }

    func contentSearch(_ req: ContentSearchRequest) async throws -> ContentSearchResponse {
        ContentSearchResponse(hits: [], indexState: .ok)
    }

    func transcriptSearch(_ req: TranscriptSearchRequest) async throws -> TranscriptSearchResponse {
        TranscriptSearchResponse(hits: [], coverage: .bestEffort)
    }

    func appsList() async throws -> AppsListResponse {
        AppsListResponse(appNames: [], hostnames: [])
    }

    func tasksList(_ req: TasksListRequest) async throws -> TasksListResponse {
        TasksListResponse(recording: req.recording, tasks: [])
    }
}
