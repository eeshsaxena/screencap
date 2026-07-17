import XCTest
@testable import ScreenCap

/// U10 (local-first intelligence) — the `tasks.list` read models, the
/// task-aware title/summary rules (`JournalModel`), and the `JournalTasks`
/// resolver's caching / fail-silent contract. Rendering is verified by
/// build-and-run; these pin the pure logic + decoding.
final class RecordingTasksTests: XCTestCase {

    // MARK: - Decoding

    func testDecodesTasksListResponse() throws {
        let json = """
        {
          "ok": true, "schema_version": 1, "daemon_version": "0.0.0",
          "api_schema_version": 1, "recording": "demo",
          "tasks": [
            {"task_index": 0, "start_ts": 100.0, "end_ts": 200.0,
             "name": "Payroll run in Gusto", "category": "finance", "confidence": "high"},
            {"task_index": 1, "start_ts": 200.0, "end_ts": 350.0, "name": "Email triage",
             "category": null, "confidence": null}
          ]
        }
        """
        let resp = try JSONDecoder().decode(TasksListResponse.self, from: Data(json.utf8))
        XCTAssertEqual(resp.recording, "demo")
        XCTAssertEqual(resp.tasks.count, 2)
        XCTAssertEqual(resp.tasks[0].taskIndex, 0)
        XCTAssertEqual(resp.tasks[0].name, "Payroll run in Gusto")
        XCTAssertEqual(resp.tasks[0].category, "finance")
        XCTAssertEqual(resp.tasks[0].confidence, "high")
        // Heuristic-style task carries no category/confidence.
        XCTAssertNil(resp.tasks[1].category)
        XCTAssertNil(resp.tasks[1].confidence)
    }

    func testDecodesEmptyTasksListGracefully() throws {
        let json = """
        {"ok": true, "schema_version": 1, "daemon_version": "0.0.0",
         "api_schema_version": 1, "recording": "empty", "tasks": []}
        """
        let resp = try JSONDecoder().decode(TasksListResponse.self, from: Data(json.utf8))
        XCTAssertEqual(resp.recording, "empty")
        XCTAssertTrue(resp.tasks.isEmpty)
        // Legacy daemon envelope: both honest-status fields absent → nil, never a throw.
        XCTAssertNil(resp.reason)
        XCTAssertNil(resp.detail)
    }

    /// SCR-275 U6/U7: the additive `reason` + `detail` pair decodes off the same
    /// envelope — `detail` is the distinct degradation reason (`context-window` =
    /// "session too long for the on-device model").
    func testDecodesReasonAndDetailAdditively() throws {
        let json = """
        {"ok": true, "schema_version": 1, "daemon_version": "0.0.0",
         "api_schema_version": 1, "recording": "r", "tasks": [],
         "reason": "produced_tasks_partial", "detail": "context-window"}
        """
        let resp = try JSONDecoder().decode(TasksListResponse.self, from: Data(json.utf8))
        XCTAssertEqual(resp.reason, "produced_tasks_partial")
        XCTAssertEqual(resp.detail, "context-window")
    }

    // MARK: - displayTitle

    func testDisplayTitleKeepsNamerTitleWhenPresent() {
        let rec = recording(name: "2026-07-06_10-00-00", title: "Payroll Reconciliation")
        let title = JournalModel.displayTitle(rec, tasks: [task(0, "Some task")])
        XCTAssertEqual(title, "Payroll Reconciliation", "a distinct recording title is never overridden by a task")
    }

    func testDisplayTitleFallsBackToFirstTaskWhenUnnamed() {
        // title == name means the recording has no distinct title (RecordingSummary
        // defaults title to the directory name).
        let rec = recording(name: "2026-07-06_10-00-00", title: nil)
        let title = JournalModel.displayTitle(rec, tasks: [task(0, "Payroll run in Gusto")])
        XCTAssertEqual(title, "Payroll run in Gusto")
    }

    func testDisplayTitleKeepsDirectoryNameWhenNoTasks() {
        let rec = recording(name: "2026-07-06_10-00-00", title: nil)
        let title = JournalModel.displayTitle(rec, tasks: [])
        XCTAssertEqual(title, "2026-07-06_10-00-00", "no tasks + no distinct title → directory name, no crash")
    }

    // MARK: - summaryLine (task-aware overload)

    func testSummaryLinePrefersNamerSummary() {
        let rec = recording(name: "r", title: "T", summary: "Ran the mid-month cycle")
        XCTAssertEqual(
            JournalModel.summaryLine(rec, tasks: [task(0, "A task")]),
            "Ran the mid-month cycle"
        )
    }

    func testSummaryLineFallsBackToTaskCountLine() {
        let rec = recording(name: "r", title: "T", summary: nil)
        XCTAssertEqual(
            JournalModel.summaryLine(rec, tasks: [task(0, "First"), task(1, "Second"), task(2, "Third")]),
            "First · +2 more"
        )
        XCTAssertEqual(
            JournalModel.summaryLine(rec, tasks: [task(0, "Only one")]),
            "Only one"
        )
    }

    func testSummaryLineNilWhenNoSummaryAndNoTasks() {
        let rec = recording(name: "r", title: "T", summary: nil)
        XCTAssertNil(JournalModel.summaryLine(rec, tasks: []))
    }

    // MARK: - JournalTasks resolver

    /// A daemon miss (throw) → tasks omitted (empty), never an error.
    @MainActor
    func testTasksOmittedOnDaemonFailure() async {
        let service = FakeTasksService()
        service.shouldThrow = true
        let resolver = JournalTasks(service: service)
        let rec = recording(name: "r1", title: nil)
        await resolver.resolve(rec)
        XCTAssertEqual(resolver.tasks(for: rec), [])
    }

    /// One query per recording, cached — including after an empty result.
    @MainActor
    func testResolvesOncePerRecording() async {
        let service = FakeTasksService()
        service.tasks = [task(0, "Payroll run in Gusto")]
        let resolver = JournalTasks(service: service)
        let rec = recording(name: "r1", title: nil)
        await resolver.resolve(rec)
        await resolver.resolve(rec)
        XCTAssertEqual(service.callCount, 1)
        XCTAssertEqual(resolver.tasks(for: rec).map(\.name), ["Payroll run in Gusto"])
    }

    // MARK: - U11 CRUD request encoding

    func testCreateRequestEncodesExpectedKeys() throws {
        let obj = try jsonObject(TasksCreateRequest(recording: "r", name: "Payroll", startTs: 100, endTs: 200))
        XCTAssertEqual(obj["recording"] as? String, "r")
        XCTAssertEqual(obj["name"] as? String, "Payroll")
        XCTAssertEqual(obj["start_ts"] as? Double, 100)
        XCTAssertEqual(obj["end_ts"] as? Double, 200)
        // Optional category omitted when nil (matches the daemon default).
        XCTAssertNil(obj["category"])
        let withCat = try jsonObject(TasksCreateRequest(recording: "r", name: "n", startTs: 1, endTs: 2, category: "finance"))
        XCTAssertEqual(withCat["category"] as? String, "finance")
    }

    func testUpdateRequestOmitsUnsetFields() throws {
        // A name-only edit sends only recording + task_index + name.
        let obj = try jsonObject(TasksUpdateRequest(recording: "r", taskIndex: 5, name: "New"))
        XCTAssertEqual(obj["task_index"] as? Int, 5)
        XCTAssertEqual(obj["name"] as? String, "New")
        XCTAssertNil(obj["start_ts"])
        XCTAssertNil(obj["end_ts"])
        XCTAssertNil(obj["category"])
    }

    func testDeleteMergeSplitRequestKeys() throws {
        let del = try jsonObject(TasksDeleteRequest(recording: "r", taskIndex: 7))
        XCTAssertEqual(del["task_index"] as? Int, 7)

        let merge = try jsonObject(TasksMergeRequest(recording: "r", taskIndices: [1, 2], name: "Merged"))
        XCTAssertEqual(merge["task_indices"] as? [Int], [1, 2])
        XCTAssertEqual(merge["name"] as? String, "Merged")

        let split = try jsonObject(TasksSplitRequest(recording: "r", taskIndex: 3, splitTs: 150, nameLeft: "L", nameRight: "R"))
        XCTAssertEqual(split["task_index"] as? Int, 3)
        XCTAssertEqual(split["split_ts"] as? Double, 150)
        XCTAssertEqual(split["name_left"] as? String, "L")
        XCTAssertEqual(split["name_right"] as? String, "R")
    }

    // MARK: - U11 CRUD response decoding

    func testCreateResponseDecodesEchoedTask() throws {
        let json = """
        {"ok": true, "schema_version": 1, "daemon_version": "0.0.0", "api_schema_version": 1,
         "recording": "demo",
         "task": {"task_index": 5000, "start_ts": 100.0, "end_ts": 200.0, "name": "Marked span",
                  "category": null, "confidence": null}}
        """
        let resp = try JSONDecoder().decode(TasksCreateResponse.self, from: Data(json.utf8))
        XCTAssertEqual(resp.recording, "demo")
        XCTAssertEqual(resp.task.taskIndex, 5000)
        XCTAssertEqual(resp.task.name, "Marked span")
    }

    func testMutatingResponsesDecode() throws {
        let upd = try JSONDecoder().decode(TasksUpdateResponse.self, from: Data("""
        {"ok": true, "schema_version": 1, "daemon_version": "d", "api_schema_version": 1, "recording": "r", "task_index": 5001}
        """.utf8))
        XCTAssertEqual(upd.taskIndex, 5001)

        let del = try JSONDecoder().decode(TasksDeleteResponse.self, from: Data("""
        {"ok": true, "schema_version": 1, "daemon_version": "d", "api_schema_version": 1, "recording": "r", "deleted": true}
        """.utf8))
        XCTAssertTrue(del.deleted)

        let merge = try JSONDecoder().decode(TasksMergeResponse.self, from: Data("""
        {"ok": true, "schema_version": 1, "daemon_version": "d", "api_schema_version": 1, "recording": "r", "task_index": 5002}
        """.utf8))
        XCTAssertEqual(merge.taskIndex, 5002)

        let split = try JSONDecoder().decode(TasksSplitResponse.self, from: Data("""
        {"ok": true, "schema_version": 1, "daemon_version": "d", "api_schema_version": 1, "recording": "r", "task_indices": [5003, 5004]}
        """.utf8))
        XCTAssertEqual(split.taskIndices, [5003, 5004])
    }

    // MARK: - U11 write-through (cache refresh + revert + retry)

    /// A successful create refreshes the cache from the store's authoritative
    /// rows — the new user task appears with the store-allocated HIGH index.
    @MainActor
    func testCreateRefreshesCache() async {
        let service = FakeTasksService()
        let store = JournalTasks(service: service)
        let ok = await store.create(recording: "r1", name: "Marked span", startTs: 100, endTs: 200)
        XCTAssertTrue(ok)
        XCTAssertEqual(store.tasksByRecording["r1"]?.map(\.name), ["Marked span"])
        XCTAssertEqual(service.createCount, 1)
        XCTAssertNil(store.writeError)
    }

    /// A successful rename re-reads the row with its new name.
    @MainActor
    func testRenameRefreshesCache() async {
        let service = FakeTasksService()
        service.store["r1"] = [task(3, "Old name")]
        let store = JournalTasks(service: service)
        let ok = await store.rename(recording: "r1", taskIndex: 3, to: "New name")
        XCTAssertTrue(ok)
        XCTAssertEqual(store.tasksByRecording["r1"]?.first?.name, "New name")
    }

    /// A failed write reverts the optimistic change to the exact pre-write cache
    /// and surfaces a retryable error (R8 — no silent divergence).
    @MainActor
    func testFailedWriteRevertsAndSurfaces() async {
        let service = FakeTasksService()
        service.store["r1"] = [task(3, "Keep me")]
        let store = JournalTasks(service: service)
        await store.refresh("r1")   // seed the cache
        service.failWrites = true

        let ok = await store.delete(recording: "r1", taskIndex: 3)
        XCTAssertFalse(ok)
        // Reverted: the task the user tried to delete is still there.
        XCTAssertEqual(store.tasksByRecording["r1"]?.map(\.name), ["Keep me"])
        XCTAssertNotNil(store.writeError, "a failed write must surface a visible error")
    }

    /// Retry replays the failed op; once the service recovers it succeeds and
    /// clears the error.
    @MainActor
    func testRetryReplaysFailedWrite() async {
        let service = FakeTasksService()
        service.store["r1"] = [task(3, "Keep me")]
        let store = JournalTasks(service: service)
        await store.refresh("r1")
        service.failWrites = true
        _ = await store.delete(recording: "r1", taskIndex: 3)
        XCTAssertNotNil(store.writeError)

        service.failWrites = false
        await store.retryLastWrite()
        XCTAssertNil(store.writeError)
        XCTAssertNil(store.tasksByRecording["r1"], "the retried delete emptied the recording's tasks")
    }

    /// The empty-day placeholder gate: after resolving an empty recording,
    /// `hasResolved` is true and `tasks(for:)` is empty — the card shows the
    /// "unsplit — still searchable" placeholder rather than a blank.
    @MainActor
    func testEmptyDayPlaceholderGate() async {
        let service = FakeTasksService()   // no store entry, empty flat list
        let store = JournalTasks(service: service)
        let rec = recording(name: "r1", title: nil)
        XCTAssertFalse(store.hasResolved(rec))
        await store.resolve(rec)
        XCTAssertTrue(store.hasResolved(rec))
        XCTAssertTrue(store.tasks(for: rec).isEmpty)
    }

    // MARK: - Fixtures

    private func jsonObject<T: Encodable>(_ value: T) throws -> [String: Any] {
        let data = try JSONEncoder().encode(value)
        return try XCTUnwrap(JSONSerialization.jsonObject(with: data) as? [String: Any])
    }

    private func task(_ index: Int, _ name: String) -> RecordingTask {
        RecordingTask(taskIndex: index, startTs: 0, endTs: 1, name: name)
    }

    private func recording(name: String, title: String?, summary: String? = nil) -> RecordingSummary {
        let titleJSON = title.map { "\"\($0)\"" } ?? "null"
        let summaryJSON = summary.map { "\"\($0)\"" } ?? "null"
        let json = """
        {"name":"\(name)","date":"2026-07-06","duration":"0:10","size_mb":"x",
         "has_audio":false,"transcribed":false,"uploaded":false,"is_stub":false,
         "chunks_total":0,"chunks_uploaded":0,"intent":null,
         "started_at":null,"duration_seconds":10,"drops":null,
         "size_bytes":0,"summary":\(summaryJSON),"title":\(titleJSON)}
        """
        return try! JSONDecoder().decode(RecordingSummary.self, from: Data(json.utf8))
    }
}

/// Search-verb fake for the tasks resolver + write-through layer (SCR-214 U11).
/// `tasksList` reads a per-recording in-memory `store` (falling back to the flat
/// `tasks` list for the pre-U11 resolver tests); the five write verbs mutate that
/// store so a write-through can be observed via a follow-up `tasksList`.
/// `failWrites` makes every write verb throw (revert-path coverage), while
/// `shouldThrow` makes the read verb throw (silent-degrade coverage).
private final class FakeTasksService: SearchService, @unchecked Sendable {
    var tasks: [RecordingTask] = []
    var store: [String: [RecordingTask]] = [:]
    var shouldThrow = false
    var failWrites = false
    private(set) var callCount = 0
    private(set) var createCount = 0
    private var nextIndex = 1000

    struct Failure: Error {}

    private func current(_ recording: String) -> [RecordingTask] {
        store[recording] ?? tasks
    }

    func tasksList(_ req: TasksListRequest) async throws -> TasksListResponse {
        callCount += 1
        if shouldThrow { throw Failure() }
        return TasksListResponse(recording: req.recording, tasks: current(req.recording))
    }

    func tasksCreate(_ req: TasksCreateRequest) async throws -> TasksCreateResponse {
        if failWrites { throw Failure() }
        createCount += 1
        var list = current(req.recording)
        let row = RecordingTask(
            taskIndex: nextIndex, startTs: req.startTs, endTs: req.endTs,
            name: req.name, category: req.category
        )
        nextIndex += 1
        list.append(row)
        store[req.recording] = list
        return TasksCreateResponse(recording: req.recording, task: row)
    }

    func tasksUpdate(_ req: TasksUpdateRequest) async throws -> TasksUpdateResponse {
        if failWrites { throw Failure() }
        var list = current(req.recording)
        if let i = list.firstIndex(where: { $0.taskIndex == req.taskIndex }) {
            let t = list[i]
            list[i] = RecordingTask(
                taskIndex: t.taskIndex,
                startTs: req.startTs ?? t.startTs,
                endTs: req.endTs ?? t.endTs,
                name: req.name ?? t.name,
                category: req.category ?? t.category,
                confidence: t.confidence
            )
            store[req.recording] = list
        }
        return TasksUpdateResponse(recording: req.recording, taskIndex: req.taskIndex)
    }

    func tasksDelete(_ req: TasksDeleteRequest) async throws -> TasksDeleteResponse {
        if failWrites { throw Failure() }
        var list = current(req.recording)
        let before = list.count
        list.removeAll { $0.taskIndex == req.taskIndex }
        store[req.recording] = list
        return TasksDeleteResponse(recording: req.recording, deleted: list.count < before)
    }

    func tasksMerge(_ req: TasksMergeRequest) async throws -> TasksMergeResponse {
        if failWrites { throw Failure() }
        var list = current(req.recording)
        let victims = list.filter { req.taskIndices.contains($0.taskIndex) }
        let lo = victims.map(\.startTs).min() ?? 0
        let hi = victims.map(\.endTs).max() ?? 0
        list.removeAll { req.taskIndices.contains($0.taskIndex) }
        let row = RecordingTask(taskIndex: nextIndex, startTs: lo, endTs: hi, name: req.name, category: req.category)
        nextIndex += 1
        list.append(row)
        store[req.recording] = list
        return TasksMergeResponse(recording: req.recording, taskIndex: row.taskIndex)
    }

    func tasksSplit(_ req: TasksSplitRequest) async throws -> TasksSplitResponse {
        if failWrites { throw Failure() }
        var list = current(req.recording)
        guard let target = list.first(where: { $0.taskIndex == req.taskIndex }) else {
            return TasksSplitResponse(recording: req.recording, taskIndices: [])
        }
        list.removeAll { $0.taskIndex == req.taskIndex }
        let a = RecordingTask(taskIndex: nextIndex, startTs: target.startTs, endTs: req.splitTs, name: req.nameLeft ?? target.name, category: target.category)
        let b = RecordingTask(taskIndex: nextIndex + 1, startTs: req.splitTs, endTs: target.endTs, name: req.nameRight ?? target.name, category: target.category)
        nextIndex += 2
        list.append(a)
        list.append(b)
        store[req.recording] = list
        return TasksSplitResponse(recording: req.recording, taskIndices: [a.taskIndex, b.taskIndex])
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
}
