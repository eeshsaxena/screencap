import XCTest
@testable import Screencap

/// SCR-214 U11 — the pure models behind the two manual task-creation entry
/// points: the retroactive two-endpoint span selection + boundary snapping
/// (Day-timeline), and the live-task close-timing rules. Rendering + wiring are
/// verified by build-and-run; these pin the logic.
final class ManualTaskCreationTests: XCTestCase {

    // MARK: - DaySpanSelection

    func testSelectionMarksTwoEndpointsInOrder() {
        var sel = DaySpanSelection()
        XCTAssertFalse(sel.isComplete)
        sel.mark(300)
        XCTAssertEqual(sel.firstMs, 300)
        XCTAssertNil(sel.range, "one endpoint is not a range yet")
        sel.mark(100)
        XCTAssertTrue(sel.isComplete)
        // Range is ordered regardless of mark order.
        XCTAssertEqual(sel.range?.startMs, 100)
        XCTAssertEqual(sel.range?.endMs, 300)
    }

    func testSelectionRejectsZeroLengthSpan() {
        var sel = DaySpanSelection()
        sel.mark(500)
        sel.mark(500)
        XCTAssertNil(sel.range, "coincident endpoints are not a valid span")
    }

    func testThirdMarkRestartsSelection() {
        var sel = DaySpanSelection()
        sel.mark(100)
        sel.mark(200)
        sel.mark(900)
        XCTAssertEqual(sel.firstMs, 900)
        XCTAssertNil(sel.secondMs)
    }

    // MARK: - DaySpanSnap

    private func base(_ recording: String, _ start: Int, _ end: Int) -> DayStripBaseTrack {
        DayStripBaseTrack(recording: recording, title: recording, startMs: start, endMs: end)
    }

    private func segment(_ recording: String, _ index: Int, _ start: Int, _ end: Int) -> DayStripSegment {
        DayStripSegment(recording: recording, taskIndex: index, name: "t\(index)", category: nil, startMs: start, endMs: end)
    }

    func testBoundariesUnionTrackAndTaskEdges() {
        let bounds = DaySpanSnap.boundaries(
            baseTracks: [base("r", 0, 1000)],
            segments: [segment("r", 0, 200, 400)]
        )
        XCTAssertEqual(bounds, [0, 200, 400, 1000])
    }

    func testSnapPullsToNearestBoundaryWithinTolerance() {
        let bounds = [0, 200_000, 400_000]
        // 30s from the 200_000ms boundary → snaps.
        XCTAssertEqual(DaySpanSnap.snap(230_000, to: bounds, toleranceMs: 60_000), 200_000)
        // 90s away → left unchanged.
        XCTAssertEqual(DaySpanSnap.snap(290_000, to: bounds, toleranceMs: 60_000), 290_000)
    }

    func testRecordingResolvedFromMidpoint() {
        let tracks = [base("morning", 0, 1000), base("afternoon", 5000, 9000)]
        XCTAssertEqual(DaySpanSnap.recording(forRangeMidpoint: 5200, 8800, baseTracks: tracks), "afternoon")
        // A span whose midpoint lands in the gap → no recording.
        XCTAssertNil(DaySpanSnap.recording(forRangeMidpoint: 1000, 5000, baseTracks: tracks))
    }

    // MARK: - LiveTask close timing

    func testLiveDraftClampsMinimumSpan() {
        let start = Date(timeIntervalSince1970: 1000)
        let draft = LiveTaskDraft(name: "x", recording: "r", startedAt: start)
        // A same-instant stop still yields a >= 1s span (never zero-length).
        XCTAssertEqual(draft.endTs(closingAt: start), 1001)
    }

    func testDayRollClosesAtEndOfStartDay() {
        var cal = Calendar(identifier: .gregorian)
        cal.timeZone = TimeZone(identifier: "UTC")!
        // Opened at 23:30 UTC, still open when the wake tick fires next morning.
        let start = cal.date(from: DateComponents(year: 2026, month: 7, day: 13, hour: 23, minute: 30))!
        let now = cal.date(from: DateComponents(year: 2026, month: 7, day: 14, hour: 8))!
        let draft = LiveTaskDraft(name: "overnight", recording: "ambient-20260713", startedAt: start)

        XCTAssertTrue(LiveTaskClose.hasDayRolled(draft: draft, now: now, calendar: cal))
        let closeAt = LiveTaskClose.closeInstant(draft: draft, reason: .dayRolled, now: now, calendar: cal)
        // Closes at the day-13 boundary (next midnight − 1s), not `now`.
        let expected = cal.date(from: DateComponents(year: 2026, month: 7, day: 14, hour: 0))!.addingTimeInterval(-1)
        XCTAssertEqual(closeAt, expected)
    }

    func testStopAndNextStartCloseAtNow() {
        let cal = Calendar(identifier: .gregorian)
        let start = Date(timeIntervalSince1970: 1000)
        let now = Date(timeIntervalSince1970: 4000)
        let draft = LiveTaskDraft(name: "x", recording: "r", startedAt: start)
        XCTAssertEqual(LiveTaskClose.closeInstant(draft: draft, reason: .stopped, now: now, calendar: cal), now)
        XCTAssertEqual(LiveTaskClose.closeInstant(draft: draft, reason: .nextTaskStarted, now: now, calendar: cal), now)
    }

    /// The live controller persists a closed span through the shared write-through
    /// layer, and starting a second task closes the first ("next task-start").
    @MainActor
    func testLiveControllerPersistsAndChainsTasks() async {
        let service = LiveFakeService()
        let store = DayTasks(service: service)
        let controller = LiveTaskController(tasks: store)

        await controller.start(name: "First", recording: "ambient-1", at: Date(timeIntervalSince1970: 1000))
        XCTAssertNotNil(controller.draft)
        // Starting a second task closes + persists the first.
        await controller.start(name: "Second", recording: "ambient-1", at: Date(timeIntervalSince1970: 2000))
        XCTAssertEqual(controller.draft?.name, "Second")
        XCTAssertEqual(service.createdNames, ["First"])

        _ = await controller.stop(at: Date(timeIntervalSince1970: 3000))
        XCTAssertNil(controller.draft)
        XCTAssertEqual(service.createdNames, ["First", "Second"])
    }
}

/// Minimal SearchService fake capturing created task names for the live-task
/// controller test.
private final class LiveFakeService: SearchService, @unchecked Sendable {
    private(set) var createdNames: [String] = []

    func tasksCreate(_ req: TasksCreateRequest) async throws -> TasksCreateResponse {
        createdNames.append(req.name)
        return TasksCreateResponse(
            recording: req.recording,
            task: RecordingTask(taskIndex: 0, startTs: req.startTs, endTs: req.endTs, name: req.name)
        )
    }
    func tasksList(_ req: TasksListRequest) async throws -> TasksListResponse {
        TasksListResponse(recording: req.recording, tasks: [])
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
    func contentSearch(_ req: ContentSearchRequest) async throws -> ContentSearchResponse {
        ContentSearchResponse(hits: [], indexState: .ok)
    }
    func transcriptSearch(_ req: TranscriptSearchRequest) async throws -> TranscriptSearchResponse {
        TranscriptSearchResponse(hits: [], coverage: .bestEffort)
    }
    func timelineQuery(_ req: TimelineQueryRequest) async throws -> TimelineQueryResponse {
        TimelineQueryResponse(rows: [], coverage: .authoritative)
    }
    func appsList() async throws -> AppsListResponse {
        AppsListResponse(appNames: [], hostnames: [])
    }
    func dayNarrative(_ req: DayNarrativeRequest) async throws -> DayNarrativeResponse {
        DayNarrativeResponse(recording: req.recording, narrative: nil)
    }
    func diarySearch(_ req: DiarySearchRequest) async throws -> DiarySearchResponse {
        DiarySearchResponse(hits: [], indexState: .ok)
    }
}
