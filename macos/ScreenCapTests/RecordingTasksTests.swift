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
    }

    // MARK: - displayTitle

    func testDisplayTitleKeepsNamerTitleWhenPresent() {
        let rec = recording(name: "2026-07-06_10-00-00", title: "Payroll Reconciliation")
        let title = JournalModel.displayTitle(rec, tasks: [task(0, "Some task")])
        XCTAssertEqual(title, "Payroll Reconciliation", "a namer title is never overridden by a task")
    }

    func testDisplayTitleFallsBackToFirstTaskWhenUnnamed() {
        // title == name means the namer produced no title (RecordingSummary
        // defaults title to the directory name).
        let rec = recording(name: "2026-07-06_10-00-00", title: nil)
        let title = JournalModel.displayTitle(rec, tasks: [task(0, "Payroll run in Gusto")])
        XCTAssertEqual(title, "Payroll run in Gusto")
    }

    func testDisplayTitleKeepsDirectoryNameWhenNoTasks() {
        let rec = recording(name: "2026-07-06_10-00-00", title: nil)
        let title = JournalModel.displayTitle(rec, tasks: [])
        XCTAssertEqual(title, "2026-07-06_10-00-00", "no tasks + no namer title → directory name, no crash")
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

    // MARK: - Fixtures

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

/// Search-verb fake for the tasks resolver — only `tasksList` carries behavior.
private final class FakeTasksService: SearchService, @unchecked Sendable {
    var tasks: [RecordingTask] = []
    var shouldThrow = false
    private(set) var callCount = 0

    struct Failure: Error {}

    func tasksList(_ req: TasksListRequest) async throws -> TasksListResponse {
        callCount += 1
        if shouldThrow { throw Failure() }
        return TasksListResponse(recording: req.recording, tasks: tasks)
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
