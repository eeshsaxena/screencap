import XCTest
@testable import Screencap

/// U8 (day diary) — the pure logic + wire decoding behind the day view's
/// narrative, block rows, thread chips, expandable bullets, and FREE-tier diary
/// search. Rendering is verified by build-and-run; these pin the model contracts:
/// forward-compatible decoding of the additive task/narrative fields, the
/// narrative composition gating, the thread-chip math, the block-id deep-link
/// (survives a re-carve), and the diary-search mapping / fail-open behavior.
final class DayDiaryTests: XCTestCase {

    // MARK: - Task wire model: additive diary fields (forward compat)

    func testRecordingTaskDecodesWithoutDiaryFields() throws {
        // An older daemon that never emits the diary fields still decodes — all
        // new fields default (empty / nil / false), never a throw.
        let json = """
        {"task_index": 0, "start_ts": 100.0, "end_ts": 200.0, "name": "Email triage",
         "category": null, "confidence": null}
        """
        let task = try JSONDecoder().decode(RecordingTask.self, from: Data(json.utf8))
        XCTAssertEqual(task.name, "Email triage")
        XCTAssertTrue(task.bullets.isEmpty)
        XCTAssertNil(task.blockId)
        XCTAssertNil(task.threadId)
        XCTAssertFalse(task.isOpen)
        XCTAssertNil(task.threadTotalMinutes)
        XCTAssertNil(task.threadSittingCount)
        XCTAssertNil(task.threadSittingIndex)
    }

    func testRecordingTaskDecodesWithDiaryFields() throws {
        let json = """
        {"task_index": 4, "start_ts": 100.0, "end_ts": 260.0, "name": "Kick drum tuning",
         "category": "audio", "confidence": "high",
         "bullets": ["Dialed the beater impact", "A/B'd against the reference"],
         "block_id": "blk-77", "thread_id": "thr-9", "is_open": true,
         "thread_total_minutes": 163.0, "thread_sitting_count": 2, "thread_sitting_index": 2}
        """
        let task = try JSONDecoder().decode(RecordingTask.self, from: Data(json.utf8))
        XCTAssertEqual(task.bullets, ["Dialed the beater impact", "A/B'd against the reference"])
        XCTAssertEqual(task.blockId, "blk-77")
        XCTAssertEqual(task.threadId, "thr-9")
        XCTAssertTrue(task.isOpen)
        XCTAssertEqual(task.threadTotalMinutes, 163.0)
        XCTAssertEqual(task.threadSittingCount, 2)
        XCTAssertEqual(task.threadSittingIndex, 2)
    }

    func testTasksQueryTaskDecodesWithAndWithoutDiaryFields() throws {
        // Without the fields (older daemon).
        let bare = """
        {"recording": "rec-a", "task_index": 1, "start_ts": 0.0, "end_ts": 1.0, "name": "n"}
        """
        let t1 = try JSONDecoder().decode(TasksQueryTask.self, from: Data(bare.utf8))
        XCTAssertTrue(t1.bullets.isEmpty)
        XCTAssertNil(t1.blockId)
        XCTAssertNil(t1.threadSittingCount)

        // With the fields, incl. the thread rollup.
        let full = """
        {"recording": "rec-a", "task_index": 1, "start_ts": 0.0, "end_ts": 1.0, "name": "n",
         "bullets": ["a"], "block_id": "b1", "thread_id": "t1", "is_open": false,
         "thread_total_minutes": 43.0, "thread_sitting_count": 2, "thread_sitting_index": 1}
        """
        let t2 = try JSONDecoder().decode(TasksQueryTask.self, from: Data(full.utf8))
        XCTAssertEqual(t2.bullets, ["a"])
        XCTAssertEqual(t2.blockId, "b1")
        XCTAssertEqual(t2.threadId, "t1")
        XCTAssertEqual(t2.threadSittingCount, 2)
        XCTAssertEqual(t2.threadSittingIndex, 1)
    }

    // MARK: - day.narrative wire decoding

    func testDayNarrativeDecodesFull() throws {
        let json = """
        {"ok": true, "schema_version": 1, "daemon_version": "0.0.0", "api_schema_version": 1,
         "recording": "rec-a", "narrative": "You spent the morning mixing.",
         "generated_at": 1719000000.0, "reason": null, "store_state": "mounted"}
        """
        let resp = try JSONDecoder().decode(DayNarrativeResponse.self, from: Data(json.utf8))
        XCTAssertEqual(resp.recording, "rec-a")
        XCTAssertEqual(resp.text, "You spent the morning mixing.")
        XCTAssertEqual(resp.generatedAt, 1719000000.0)
        XCTAssertNil(resp.reason)
        XCTAssertTrue(resp.resolvedStoreState.isMounted)
    }

    func testDayNarrativeDecodesAbsentNarrativeAndOlderDaemon() throws {
        // A mechanical-only day: null narrative, plus an older daemon that omits
        // generated_at / reason / store_state — all decode tolerantly.
        let json = """
        {"ok": true, "schema_version": 1, "daemon_version": "0.0.0", "api_schema_version": 1,
         "recording": "rec-b", "narrative": null}
        """
        let resp = try JSONDecoder().decode(DayNarrativeResponse.self, from: Data(json.utf8))
        XCTAssertNil(resp.text)
        XCTAssertNil(resp.generatedAt)
        XCTAssertTrue(resp.resolvedStoreState.isMounted)  // absent store_state → mounted
    }

    func testDayNarrativeBlankNarrativeTreatedAsNone() throws {
        // A present-but-whitespace narrative must NOT render as an empty section.
        let resp = DayNarrativeResponse(recording: "r", narrative: "   \n ")
        XCTAssertNil(resp.text)
    }

    func testDayNarrativeSealedStoreDecodesDegraded() throws {
        let json = """
        {"ok": true, "schema_version": 1, "daemon_version": "0.0.0", "api_schema_version": 1,
         "recording": "rec-a", "narrative": null, "store_state": "locked"}
        """
        let resp = try JSONDecoder().decode(DayNarrativeResponse.self, from: Data(json.utf8))
        XCTAssertEqual(resp.resolvedStoreState, .locked)
    }

    // MARK: - Narrative composition (KTD-10, R8/R9)

    func testComposeShowsNarrativeWhenPresent() {
        let responses = [DayNarrativeResponse(recording: "r", narrative: "A written day.")]
        XCTAssertEqual(
            DayNarrativeComposition.compose(responses: responses, hasBlocks: true),
            .narrative(text: "A written day.")
        )
    }

    func testComposeMixedDayIsPartialCoveringOnlyRealNarratives() {
        // A mixed day: one produced-tasks recording narrated, one mechanical-only
        // wrote none → the section shows ONLY the real half (partial narrative).
        let responses = [
            DayNarrativeResponse(recording: "real", narrative: "Mixed the record."),
            DayNarrativeResponse(recording: "mechanical", narrative: nil),
        ]
        XCTAssertEqual(
            DayNarrativeComposition.compose(responses: responses, hasBlocks: true),
            .narrative(text: "Mixed the record.")
        )
    }

    func testComposeJoinsMultipleNarrativesInOrder() {
        let responses = [
            DayNarrativeResponse(recording: "am", narrative: "Morning work."),
            DayNarrativeResponse(recording: "pm", narrative: "Afternoon work."),
        ]
        XCTAssertEqual(
            DayNarrativeComposition.compose(responses: responses, hasBlocks: true),
            .narrative(text: "Morning work.\n\nAfternoon work.")
        )
    }

    func testComposeStillComposingWhenBlocksButNoNarrative() {
        // Real blocks exist but no narrative row yet → the DISTINCT "still
        // composing" state, never a blank slot (R10).
        let responses = [DayNarrativeResponse(recording: "r", narrative: nil)]
        XCTAssertEqual(
            DayNarrativeComposition.compose(responses: responses, hasBlocks: true),
            .stillComposing
        )
    }

    func testComposeHiddenWhenNoNarrativeAndNoBlocks() {
        // Mechanical-only / absent / legacy day: no blocks + no narrative → the
        // section is omitted entirely (defers to the strip's honest states).
        let responses = [DayNarrativeResponse(recording: "r", narrative: nil)]
        XCTAssertEqual(
            DayNarrativeComposition.compose(responses: responses, hasBlocks: false),
            .hidden
        )
        // And an entirely empty day (no recordings) is also hidden.
        XCTAssertEqual(
            DayNarrativeComposition.compose(responses: [], hasBlocks: false),
            .hidden
        )
    }

    // MARK: - Thread chip math (R7)

    func testThreadChipTextForThreadOfTwo() {
        XCTAssertEqual(
            TasksModel.threadChipText(sittingIndex: 2, sittingCount: 2, totalMinutes: 163),
            "2 of 2 · 2h43 today"
        )
    }

    func testThreadChipTextNilForLoneBlockOrMissingRollup() {
        // A lone block (count < 2) → no chip.
        XCTAssertNil(TasksModel.threadChipText(sittingIndex: 1, sittingCount: 1, totalMinutes: 30))
        // An older daemon that omits the rollup → no chip.
        XCTAssertNil(TasksModel.threadChipText(sittingIndex: nil, sittingCount: nil, totalMinutes: nil))
        XCTAssertNil(TasksModel.threadChipText(sittingIndex: 2, sittingCount: 2, totalMinutes: nil))
    }

    func testThreadDurationFormatting() {
        XCTAssertEqual(TasksModel.threadDurationText(minutes: 163), "2h43")
        XCTAssertEqual(TasksModel.threadDurationText(minutes: 120), "2h")
        XCTAssertEqual(TasksModel.threadDurationText(minutes: 43), "43m")
        XCTAssertEqual(TasksModel.threadDurationText(minutes: 5), "5m")
        XCTAssertEqual(TasksModel.threadDurationText(minutes: 0), "0m")
    }

    func testThreadSiblingResolution() {
        let day = Date(timeIntervalSince1970: 1_719_000_000)
        let a = row(recording: "r", taskIndex: 1, name: "Mixing", day: day, blockId: "b1", threadId: "t1",
                    threadTotalMinutes: 163, threadSittingCount: 2, threadSittingIndex: 1)
        let b = row(recording: "r", taskIndex: 2, name: "Mixing", day: day, blockId: "b2", threadId: "t1",
                    threadTotalMinutes: 163, threadSittingCount: 2, threadSittingIndex: 2)
        let unrelated = row(recording: "r", taskIndex: 3, name: "Email", day: day, blockId: "b3")
        let rows = [a, b, unrelated]

        // From the first sitting → the next sitting.
        XCTAssertEqual(TasksModel.threadSiblingId(in: rows, from: a), b.id)
        // From the last sitting → wraps to the first.
        XCTAssertEqual(TasksModel.threadSiblingId(in: rows, from: b), a.id)
        // A non-threaded row has no sibling.
        XCTAssertNil(TasksModel.threadSiblingId(in: rows, from: unrelated))
    }

    // MARK: - Deep-link by block_id survives a re-carve (KTD-2)

    func testBlockSpanResolvesByBlockIdAfterReCarveChangesTaskIndex() {
        let day = Date(timeIntervalSince1970: 1_719_000_000)
        // Pre-carve: block "blk-1" is task_index 2 over [100, 260]s.
        let pre = [
            row(recording: "r", taskIndex: 2, name: "Mixing", day: day, startTs: 100, endTs: 260, blockId: "blk-1"),
            row(recording: "r", taskIndex: 3, name: "Email", day: day, startTs: 300, endTs: 360, blockId: "blk-2"),
        ]
        // Post-carve: the SAME block "blk-1" is renumbered to task_index 7 and
        // its span nudged to [90, 280]s — deep links must still resolve it.
        let post = [
            row(recording: "r", taskIndex: 7, name: "Mixing", day: day, startTs: 90, endTs: 280, blockId: "blk-1"),
            row(recording: "r", taskIndex: 8, name: "Email", day: day, startTs: 300, endTs: 360, blockId: "blk-2"),
        ]
        let preSpan = TasksModel.blockSpan(in: pre, blockId: "blk-1")
        let postSpan = TasksModel.blockSpan(in: post, blockId: "blk-1")
        XCTAssertEqual(preSpan?.startMs, 100_000)
        XCTAssertEqual(postSpan?.startMs, 90_000)   // resolved by block_id, not task_index
        XCTAssertEqual(postSpan?.endMs, 280_000)
        XCTAssertNil(TasksModel.blockSpan(in: post, blockId: "missing"))
    }

    // MARK: - Expandable bullets render only when present (data gate)

    func testBulletsGateFromModel() {
        let day = Date(timeIntervalSince1970: 1_719_000_000)
        let withBullets = row(recording: "r", taskIndex: 1, name: "n", day: day, bullets: ["one", "two"])
        let without = row(recording: "r", taskIndex: 2, name: "n", day: day)
        XCTAssertEqual(withBullets.bullets.count, 2)
        XCTAssertTrue(without.bullets.isEmpty)  // the view hides the disclosure for this row
    }

    // MARK: - Local filter matches bullets (R5)

    func testFilterMatchesBulletText() {
        let day = Date(timeIntervalSince1970: 1_719_000_000)
        let group = TasksModel.DayGroup(
            date: "2026-07-18",
            label: "Today",
            rows: [
                row(recording: "r", taskIndex: 1, name: "Studio session", day: day,
                    bullets: ["Tuned the kick drum for the chorus"])
            ]
        )
        // The NAME doesn't contain "kick", but a bullet does → the row survives.
        let filtered = TasksModel.filter([group], query: "kick")
        XCTAssertEqual(filtered.count, 1)
        XCTAssertEqual(filtered.first?.rows.count, 1)
        // A word in neither name nor bullets drops the day.
        XCTAssertTrue(TasksModel.filter([group], query: "invoice").isEmpty)
    }

    // MARK: - diary.search wire + mapping (R5/KTD-8)

    func testDiarySearchResponseDecodes() throws {
        let json = """
        {"ok": true, "schema_version": 1, "daemon_version": "0.0.0", "api_schema_version": 1,
         "hits": [
           {"recording": "rec-a", "block_id": "blk-1", "start_ms": 1719000000000,
            "end_ms": 1719003600000, "snippet": "kick drum tuning", "score": -1.2}
         ], "index_state": "ok", "store_state": "mounted"}
        """
        let resp = try JSONDecoder().decode(DiarySearchResponse.self, from: Data(json.utf8))
        XCTAssertEqual(resp.hits.count, 1)
        XCTAssertEqual(resp.hits[0].blockId, "blk-1")
        XCTAssertEqual(resp.hits[0].snippet, "kick drum tuning")
        XCTAssertEqual(resp.indexState, .ok)
        XCTAssertTrue(resp.resolvedStoreState.isMounted)
    }

    func testDiarySearchResponseUnknownIndexStateDefaultsPessimistic() throws {
        let json = """
        {"ok": true, "schema_version": 1, "daemon_version": "0.0.0", "api_schema_version": 1,
         "hits": [], "index_state": "some_future_state"}
        """
        let resp = try JSONDecoder().decode(DiarySearchResponse.self, from: Data(json.utf8))
        XCTAssertEqual(resp.indexState, .storeUnavailable)
    }

    func testDiaryResultRowMapping() {
        let startMs = 1_719_000_000_000
        let hits = [DiaryBlockHit(recording: "rec-a", blockId: "blk-1", startMs: startMs,
                                  endMs: startMs + 3_600_000, snippet: "kick drum tuning")]
        let cal = Calendar(identifier: .gregorian)
        let rows = TasksModel.diaryResultRows(from: hits, now: Date(timeIntervalSince1970: 1_719_100_000), calendar: cal)
        XCTAssertEqual(rows.count, 1)
        XCTAssertEqual(rows[0].blockId, "blk-1")
        XCTAssertEqual(rows[0].snippet, "kick drum tuning")
        XCTAssertEqual(rows[0].startMs, startMs)
        XCTAssertEqual(rows[0].id, "rec-a#blk-1")
        XCTAssertEqual(rows[0].highlight.startMs, startMs)
        XCTAssertEqual(rows[0].day, cal.startOfDay(for: Date(timeIntervalSince1970: Double(startMs) / 1000)))
        XCTAssertFalse(rows[0].dayLabel.isEmpty)
    }

    // MARK: - DiarySearchModel (FREE-tier history reach, fail-open)

    @MainActor
    func testDiarySearchModelMapsHits() async {
        let fake = FakeDiaryService()
        fake.diaryHits = [DiaryBlockHit(recording: "rec-a", blockId: "blk-1", startMs: 1_719_000_000_000,
                                        endMs: 1_719_003_600_000, snippet: "design systems talk")]
        let model = DiarySearchModel(service: fake)
        await model.search("design systems")
        XCTAssertEqual(model.results.count, 1)
        XCTAssertEqual(model.results.first?.blockId, "blk-1")
        XCTAssertFalse(model.searching)
    }

    @MainActor
    func testDiarySearchModelBlankQueryClearsWithoutHittingDaemon() async {
        let fake = FakeDiaryService()
        fake.diaryHits = [DiaryBlockHit(recording: "r", blockId: "b", startMs: 0, endMs: 1, snippet: "x")]
        let model = DiarySearchModel(service: fake)
        await model.search("   ")
        XCTAssertTrue(model.results.isEmpty)
        XCTAssertEqual(fake.diaryCallCount, 0)  // never queried for a blank needle
    }

    @MainActor
    func testDiarySearchModelFailsOpenOnThrow() async {
        let fake = FakeDiaryService()
        fake.throwDiary = true
        let model = DiarySearchModel(service: fake)
        await model.search("anything")
        XCTAssertTrue(model.results.isEmpty)   // degrades to no history results, never an error
        XCTAssertFalse(model.searching)
    }

    // MARK: - Fixtures

    private func row(
        recording: String,
        taskIndex: Int,
        name: String,
        day: Date,
        startTs: Double = 0,
        endTs: Double = 1,
        category: String? = nil,
        bullets: [String] = [],
        blockId: String? = nil,
        threadId: String? = nil,
        isOpen: Bool = false,
        threadTotalMinutes: Double? = nil,
        threadSittingCount: Int? = nil,
        threadSittingIndex: Int? = nil
    ) -> TasksModel.TaskRow {
        TasksModel.TaskRow(
            recording: recording, recordingId: nil, taskIndex: taskIndex, name: name,
            category: category, startTs: startTs, endTs: endTs, day: day,
            bullets: bullets, blockId: blockId, threadId: threadId, isOpen: isOpen,
            threadTotalMinutes: threadTotalMinutes, threadSittingCount: threadSittingCount,
            threadSittingIndex: threadSittingIndex
        )
    }
}

/// Diary-verb fake for `DiarySearchModel` (U8). Serves configurable diary hits
/// (or throws) and stubs the rest of the `SearchService` seam so it conforms.
private final class FakeDiaryService: SearchService, @unchecked Sendable {
    var diaryHits: [DiaryBlockHit] = []
    var throwDiary = false
    private(set) var diaryCallCount = 0

    struct Failure: Error {}

    func diarySearch(_ req: DiarySearchRequest) async throws -> DiarySearchResponse {
        diaryCallCount += 1
        if throwDiary { throw Failure() }
        return DiarySearchResponse(hits: diaryHits, indexState: .ok)
    }

    func dayNarrative(_ req: DayNarrativeRequest) async throws -> DayNarrativeResponse {
        DayNarrativeResponse(recording: req.recording, narrative: nil)
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
