import XCTest
@testable import ScreenCap

/// U3 — the Days surface's pure model (`DaysModel`): day-clamped coverage
/// grouping (KTD-6), day labels, the Today-card live capture status (AE6), the
/// day-level upload/review badge (R14), the task-summary line, and the app-chip
/// resolver's caching / fail-silent contract (`DayAppChips`). Rendering is
/// verified by build-and-run.
final class DaysModelTests: XCTestCase {

    // MARK: - Day-clamped coverage (KTD-6)

    /// Today / Yesterday / older group by covered day, newest day first.
    func testGroupsAcrossDaysNewestFirst() {
        let cal = Calendar.current
        let now = Date()
        let today = cal.startOfDay(for: now)
        let cards = DaysModel.dayCards([
            rec(name: "old", startedAt: today.addingTimeInterval(-5 * 86_400 + 9 * 3_600)),
            rec(name: "today", startedAt: today.addingTimeInterval(10 * 3_600)),
            rec(name: "yesterday", startedAt: today.addingTimeInterval(-86_400 + 15 * 3_600)),
        ], now: now, calendar: cal)

        XCTAssertEqual(cards.count, 3)
        XCTAssertEqual(cards[0].label, "Today")
        XCTAssertTrue(cards[0].isToday)
        XCTAssertEqual(cards[0].recordings.map(\.name), ["today"])
        XCTAssertEqual(cards[1].label, "Yesterday")
        XCTAssertEqual(cards[2].recordings.map(\.name), ["old"])
    }

    /// KTD-6: a 23:50 recording running past midnight gets a card on BOTH its
    /// start day AND the following day — an overnight tail is never lost, and the
    /// day it spills into still gets a card (this is the change from start-day
    /// bucketing).
    func testOvernightTailGetsCardsOnBothDays() {
        let cal = Calendar.current
        let now = Date()
        let today = cal.startOfDay(for: now)
        // Started yesterday 23:50, 20 minutes long — ends today 00:10.
        let cards = DaysModel.dayCards(
            [rec(name: "overnight", startedAt: today.addingTimeInterval(-600), durationSeconds: 1_200)],
            now: now, calendar: cal
        )
        XCTAssertEqual(cards.map(\.label), ["Today", "Yesterday"])
        XCTAssertTrue(cards.allSatisfy { $0.recordings.map(\.name) == ["overnight"] })
    }

    /// A recording confined to one day covers exactly that day.
    func testSingleDayRecordingCoversOneDay() {
        let cal = Calendar.current
        let now = Date()
        let today = cal.startOfDay(for: now)
        let days = DaysModel.coveredDays(
            for: rec(name: "r", startedAt: today.addingTimeInterval(10 * 3_600), durationSeconds: 3_600),
            calendar: cal
        )
        XCTAssertEqual(days, [today])
    }

    /// An undated recording (no derivable start) yields NO day card — it is
    /// reachable through Inspect (KTD-10), never bucketed into a fabricated date.
    func testUndatedRecordingProducesNoCard() {
        let cal = Calendar.current
        let now = Date()
        let today = cal.startOfDay(for: now)
        let cards = DaysModel.dayCards([
            rec(name: "dated", startedAt: today.addingTimeInterval(3_600)),
            rec(name: "mystery", startedAt: nil, date: "not-a-date"),
        ], now: now, calendar: cal)
        XCTAssertEqual(cards.map(\.label), ["Today"])
        XCTAssertEqual(cards.first?.recordings.map(\.name), ["dated"])
    }

    // MARK: - Labels

    func testOlderDayLabelsUseTheSerifHeadingVocabulary() {
        var cal = Calendar(identifier: .gregorian)
        cal.timeZone = TimeZone(identifier: "UTC")!
        let now = date(cal, 2026, 7, 3)
        XCTAssertEqual(DaysModel.label(for: date(cal, 2026, 7, 3), now: now, calendar: cal), "Today")
        XCTAssertEqual(DaysModel.label(for: date(cal, 2026, 7, 2), now: now, calendar: cal), "Yesterday")
        XCTAssertEqual(DaysModel.label(for: date(cal, 2026, 7, 1), now: now, calendar: cal), "Wednesday, 1 July")
        XCTAssertEqual(
            DaysModel.label(for: date(cal, 2025, 12, 31), now: now, calendar: cal),
            "Wednesday, 31 December 2025",
            "days outside the current year carry the year"
        )
    }

    // MARK: - Today card status (R15 / AE6)

    /// AE6: ambient on + a live recording → "Recording since"; ambient off →
    /// "off"; ambient paused mid-recording → a DISTINCT paused state (not
    /// "Recording since", not "off").
    func testTodayStatusDistinguishesRecordingPausedAndOff() {
        let since = Date(timeIntervalSince1970: 1_000_000)
        // Recording.
        XCTAssertEqual(
            DaysModel.todayCaptureStatus(isRecording: true, startedAt: since,
                                         ambientEnabled: true, ambientActive: true, ambientPaused: false),
            .recording(since: since)
        )
        // Paused — distinct from recording AND off, even though a recording is live.
        XCTAssertEqual(
            DaysModel.todayCaptureStatus(isRecording: true, startedAt: since,
                                         ambientEnabled: true, ambientActive: true, ambientPaused: true),
            .paused
        )
        // Off — ambient not enabled and nothing recording.
        XCTAssertEqual(
            DaysModel.todayCaptureStatus(isRecording: false, startedAt: nil,
                                         ambientEnabled: false, ambientActive: false, ambientPaused: false),
            .off
        )
        // Enabled but not yet active — an honest "starting", never "off".
        XCTAssertEqual(
            DaysModel.todayCaptureStatus(isRecording: false, startedAt: nil,
                                         ambientEnabled: true, ambientActive: false, ambientPaused: false),
            .starting
        )
    }

    /// The paused and off status lines are distinct copy (AE6).
    func testTodayStatusLinesAreDistinct() {
        XCTAssertEqual(
            DaysModel.todayStatusLine(.recording(since: Date(timeIntervalSince1970: 0))),
            "Recording since \(hhmm(0))"
        )
        XCTAssertTrue(DaysModel.todayStatusLine(.paused).localizedCaseInsensitiveContains("paused"))
        XCTAssertEqual(DaysModel.todayStatusLine(.off), "Ambient recording is off")
        XCTAssertNotEqual(DaysModel.todayStatusLine(.paused), DaysModel.todayStatusLine(.off))
    }

    // MARK: - Upload / review badge (R14)

    /// The upload arm counts ONLY cloud-destined, upload-eligible recordings — a
    /// local-only-policy day carries no pending-upload badge.
    func testUploadReviewCountsCloudDestinedOnly() {
        let status = DaysModel.uploadReview([
            rec(name: "cloud-pending", startedAt: nil, intent: "cloud"),
            rec(name: "both-pending", startedAt: nil, intent: "both"),
            rec(name: "local", startedAt: nil, intent: "local"),
            rec(name: "cloud-done", startedAt: nil, intent: "cloud", uploaded: true),
        ])
        XCTAssertEqual(status.cloudPendingCount, 2)
        XCTAssertTrue(status.isActionable)
        XCTAssertNotNil(status.reviewTarget)
        XCTAssertNotNil(status.badgeText)
    }

    /// A day with only local-destined footage yields no badge (R14): never a
    /// permanent "not yet uploaded".
    func testLocalOnlyDayHasNoUploadBadge() {
        let status = DaysModel.uploadReview([
            rec(name: "a", startedAt: nil, intent: "local"),
            rec(name: "b", startedAt: nil, intent: nil),
        ])
        XCTAssertEqual(status.cloudPendingCount, 0)
        XCTAssertFalse(status.isActionable)
        XCTAssertNil(status.badgeText)
    }

    // MARK: - Task summary line

    func testTaskSummaryLine() {
        XCTAssertNil(DaysModel.taskSummaryLine([]))
        XCTAssertNil(DaysModel.taskSummaryLine(["  ", ""]))
        XCTAssertEqual(DaysModel.taskSummaryLine(["Only one"]), "Only one")
        XCTAssertEqual(DaysModel.taskSummaryLine(["First", "Second", "Third"]), "First · +2 more")
    }

    // MARK: - Dominant app

    func testDominantAppPicksMostFrequentFirstSeenOnTies() {
        let zoomTwice = [row(app: "Safari"), row(app: "Zoom"), row(app: "Zoom")]
        XCTAssertEqual(DaysModel.dominantApp(zoomTwice), "Zoom")
        let tie = [row(app: "Safari"), row(app: "Zoom"), row(app: "Zoom"), row(app: "Safari")]
        XCTAssertEqual(DaysModel.dominantApp(tie), "Safari", "first-seen wins a tie")
        XCTAssertNil(DaysModel.dominantApp([row(app: nil), row(app: "")]))
        XCTAssertNil(DaysModel.dominantApp([]))
    }

    // MARK: - App-chip resolver

    /// timeline.query failure → chip omitted (nullable contract), never an error.
    @MainActor
    func testAppChipOmittedOnTimelineQueryFailure() async {
        let service = FakeDaysSearchService()
        service.shouldThrow = true
        let chips = DayAppChips(service: service)
        let recording = rec(name: "r1", startedAt: nil)
        await chips.resolve(recording)
        XCTAssertNil(chips.app(for: recording))
    }

    /// One query per recording, cached — including after a failure.
    @MainActor
    func testAppChipResolvesOncePerRecording() async {
        let service = FakeDaysSearchService()
        service.rows = [row(app: "Gusto"), row(app: "Gusto"), row(app: "Slack")]
        let chips = DayAppChips(service: service)
        let recording = rec(name: "r1", startedAt: nil)
        await chips.resolve(recording)
        await chips.resolve(recording)
        XCTAssertEqual(service.timelineCalls, 1)
        XCTAssertEqual(chips.app(for: recording), "Gusto")
    }

    // MARK: - Fixtures

    private func hhmm(_ ts: TimeInterval) -> String {
        let f = DateFormatter()
        f.dateFormat = "HH:mm"
        f.timeZone = .current
        return f.string(from: Date(timeIntervalSince1970: ts))
    }

    private func rec(
        name: String,
        startedAt: Date?,
        durationSeconds: Double = 300,
        intent: String? = nil,
        uploaded: Bool = false,
        date: String = "2026-07-03"
    ) -> RecordingSummary {
        let started = startedAt.map { String($0.timeIntervalSince1970) } ?? "null"
        let intentJSON = intent.map { "\"\($0)\"" } ?? "null"
        let json = """
        {"name":"\(name)","date":"\(date)","duration":"0:10","size_mb":"x",
         "has_audio":false,"transcribed":false,"uploaded":\(uploaded),"is_stub":false,
         "chunks_total":0,"chunks_uploaded":0,"intent":\(intentJSON),
         "started_at":\(started),"duration_seconds":\(durationSeconds),"drops":null,
         "size_bytes":0}
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
private final class FakeDaysSearchService: SearchService, @unchecked Sendable {
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

    func tasksCreate(_ req: TasksCreateRequest) async throws -> TasksCreateResponse {
        TasksCreateResponse(
            recording: req.recording,
            task: RecordingTask(taskIndex: 0, startTs: req.startTs, endTs: req.endTs, name: req.name)
        )
    }
    func tasksUpdate(_ req: TasksUpdateRequest) async throws -> TasksUpdateResponse {
        TasksUpdateResponse(recording: req.recording, taskIndex: req.taskIndex)
    }
    func tasksDelete(_ req: TasksDeleteRequest) async throws -> TasksDeleteResponse {
        TasksDeleteResponse(recording: req.recording, deleted: true)
    }
    func tasksMerge(_ req: TasksMergeRequest) async throws -> TasksMergeResponse {
        TasksMergeResponse(recording: req.recording, taskIndex: 0)
    }
    func tasksSplit(_ req: TasksSplitRequest) async throws -> TasksSplitResponse {
        TasksSplitResponse(recording: req.recording, taskIndices: [0, 1])
    }
}
