import Foundation

// SCR-174 — test seam for the search verbs, mirroring `DaemonSessionService`
// (the established repo pattern for wrapping `DaemonClient` behind a protocol).
// `SearchViewModel` (U4) depends on this protocol so it can be unit-tested
// against a fake without a live UNIX socket.

protocol SearchService: Sendable {
    func contentSearch(_ req: ContentSearchRequest) async throws -> ContentSearchResponse
    func transcriptSearch(_ req: TranscriptSearchRequest) async throws -> TranscriptSearchResponse
    func timelineQuery(_ req: TimelineQueryRequest) async throws -> TimelineQueryResponse
    /// Query-parser vocabulary (SCR-179). A throw (incl. an older daemon's 404)
    /// must degrade gracefully to the static parser vocabulary at the call site.
    func appsList() async throws -> AppsListResponse
    /// A LOCAL recording's named task segments (U10). A throw (incl. an older
    /// daemon's 404) must degrade gracefully — the task breakdown is omitted, not
    /// surfaced as an error.
    func tasksList(_ req: TasksListRequest) async throws -> TasksListResponse

    // SCR-214 U11 — the task-curation write verbs, on the same seam as
    // `tasksList` so `DayTasks` write-through injects a single fake in tests.
    // Unlike the read verbs a throw here is NOT swallowed: a failed write must
    // surface a visible retry/error and revert optimistic state (R8), so the
    // caller inspects the throw rather than degrading silently.
    func tasksCreate(_ req: TasksCreateRequest) async throws -> TasksCreateResponse
    func tasksUpdate(_ req: TasksUpdateRequest) async throws -> TasksUpdateResponse
    func tasksDelete(_ req: TasksDeleteRequest) async throws -> TasksDeleteResponse
    func tasksMerge(_ req: TasksMergeRequest) async throws -> TasksMergeResponse
    func tasksSplit(_ req: TasksSplitRequest) async throws -> TasksSplitResponse

    // U8 (day diary) — the two read-only diary verbs, on the same seam so the day
    // view + Tasks surface inject a single fake in tests. Both are read verbs: a
    // throw (incl. an older daemon's 404) degrades gracefully at the call site —
    // the narrative section is omitted, and diary search falls back to the local
    // substring filter — never surfaced as an error.
    func dayNarrative(_ req: DayNarrativeRequest) async throws -> DayNarrativeResponse
    func diarySearch(_ req: DiarySearchRequest) async throws -> DiarySearchResponse
}

/// Live implementation: forwards to the daemon over the UNIX socket.
struct LiveSearchService: SearchService {
    func contentSearch(_ req: ContentSearchRequest) async throws -> ContentSearchResponse {
        try await DaemonClient.contentSearch(req)
    }

    func transcriptSearch(_ req: TranscriptSearchRequest) async throws -> TranscriptSearchResponse {
        try await DaemonClient.transcriptSearch(req)
    }

    func timelineQuery(_ req: TimelineQueryRequest) async throws -> TimelineQueryResponse {
        try await DaemonClient.timelineQuery(req)
    }

    func appsList() async throws -> AppsListResponse {
        try await DaemonClient.appsList()
    }

    func tasksList(_ req: TasksListRequest) async throws -> TasksListResponse {
        try await DaemonClient.tasksList(req)
    }

    func tasksCreate(_ req: TasksCreateRequest) async throws -> TasksCreateResponse {
        try await DaemonClient.tasksCreate(req)
    }

    func tasksUpdate(_ req: TasksUpdateRequest) async throws -> TasksUpdateResponse {
        try await DaemonClient.tasksUpdate(req)
    }

    func tasksDelete(_ req: TasksDeleteRequest) async throws -> TasksDeleteResponse {
        try await DaemonClient.tasksDelete(req)
    }

    func tasksMerge(_ req: TasksMergeRequest) async throws -> TasksMergeResponse {
        try await DaemonClient.tasksMerge(req)
    }

    func tasksSplit(_ req: TasksSplitRequest) async throws -> TasksSplitResponse {
        try await DaemonClient.tasksSplit(req)
    }

    func dayNarrative(_ req: DayNarrativeRequest) async throws -> DayNarrativeResponse {
        try await DaemonClient.dayNarrative(req)
    }

    func diarySearch(_ req: DiarySearchRequest) async throws -> DiarySearchResponse {
        try await DaemonClient.diarySearch(req)
    }
}
