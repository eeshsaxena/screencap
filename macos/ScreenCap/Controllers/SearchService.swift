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
}
