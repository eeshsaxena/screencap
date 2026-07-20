import Foundation

// SCR-174 — pointer-only result models for in-app "ask your history" search.
// These mirror the daemon's SCR-118 query verbs (`/v0/content.search`,
// `/v0/transcript.search`, `/v0/timeline.query`). By shape they are POINTER
// ONLY — recording dir name + a time/chunk pointer + a text snippet — never a
// media path or image bytes (R8). The deep-link (U6) opens the Review window
// from `(recording, timestampMs)`.

/// On-screen content index state, mirrored from the daemon's
/// `content_index.IndexState`. Decoded tolerantly: an unknown or missing value
/// defaults to `.storeUnavailable` (pessimistic — never silently "ok"/"no
/// match"), mirroring the MCP server's defaulting. Returned by
/// `content.search` only; transcript/timeline expose `coverage` instead.
enum ContentIndexState: String, Sendable, Equatable {
    case ok
    case noMatch = "no_match"
    case notIndexed = "not_indexed"
    case indexDegraded = "index_degraded"
    case storeUnavailable = "store_unavailable"

    init(wire: String?) {
        self = ContentIndexState(rawValue: wire ?? "") ?? .storeUnavailable
    }
}

/// Per-stream coverage, from the `coverage` field on transcript/timeline
/// responses. `timeline.query` is authoritative; `transcript.search` is
/// best-effort. Unknown/missing defaults to `.bestEffort` so the UI never
/// overstates how complete a stream is.
enum SearchCoverage: String, Sendable, Equatable {
    case authoritative
    case bestEffort = "best_effort"

    init(wire: String?) {
        self = SearchCoverage(rawValue: wire ?? "") ?? .bestEffort
    }
}

/// A single on-screen-text (OCR) match. Pointer: `(recording, timestampMs)`.
struct ContentHit: Decodable, Sendable, Hashable {
    let recording: String
    let timestampMs: Int
    let snippet: String
    let score: Double
    /// Which stream produced the hit: `"content"` (an on-screen-text frame match
    /// whose `timestampMs` is a real frame pointer) or `"title"` (a user-set
    /// recording-title match the daemon unions into `content.search`, whose
    /// `timestampMs` is the sentinel `0` — NOT a frame pointer). Optional so an
    /// older daemon that omits it decodes to `nil` (treated as `"content"`).
    /// Optional with no inline default: synthesized `Decodable` decodes it via
    /// `decodeIfPresent` (absent -> nil); the memberwise initializer takes it
    /// explicitly (test call sites pass `matchSource: nil` for content hits).
    let matchSource: String?

    enum CodingKeys: String, CodingKey {
        case recording
        case timestampMs = "timestamp_ms"
        case snippet
        case score
        case matchSource = "match_source"
    }
}

/// A transcript (audio) match. Pointer is chunk-granular: `chunk_index`, with
/// **no `timestamp_ms`** by daemon contract (the fine per-word timestamps live
/// in a rich JSON the daemon refuses to read). Placing a transcript hit on the
/// timeline requires resolving `chunkIndex` → an absolute chunk-start time;
/// see `SearchViewModel` (U4).
struct TranscriptHit: Decodable, Sendable, Hashable {
    let recording: String
    let chunkIndex: Int
    let snippet: String

    enum CodingKeys: String, CodingKey {
        case recording
        case chunkIndex = "chunk_index"
        case snippet
    }
}

/// A structured app/window/time row from the event tables (authoritative).
/// `browser_url` is deliberately omitted by the daemon in v1.
struct TimelineRow: Decodable, Sendable, Hashable {
    let recording: String
    let timestampMs: Int
    let app: String?
    let title: String?

    enum CodingKeys: String, CodingKey {
        case recording
        case timestampMs = "timestamp_ms"
        case app
        case title
    }
}

// MARK: - Response envelopes

/// `content.search` response. `indexState` applies pessimistic defaulting via
/// `ContentIndexState(wire:)` over the raw wire value.
struct ContentSearchResponse: Decodable, Sendable {
    let ok: Bool
    let schemaVersion: Int
    let daemonVersion: String
    let apiSchemaVersion: Int
    let hits: [ContentHit]
    private let indexStateRaw: String?

    var indexState: ContentIndexState { ContentIndexState(wire: indexStateRaw) }

    init(
        ok: Bool = true, schemaVersion: Int = 1, daemonVersion: String = "test",
        apiSchemaVersion: Int = 1, hits: [ContentHit], indexState: ContentIndexState
    ) {
        self.ok = ok
        self.schemaVersion = schemaVersion
        self.daemonVersion = daemonVersion
        self.apiSchemaVersion = apiSchemaVersion
        self.hits = hits
        self.indexStateRaw = indexState.rawValue
    }

    enum CodingKeys: String, CodingKey {
        case ok
        case schemaVersion = "schema_version"
        case daemonVersion = "daemon_version"
        case apiSchemaVersion = "api_schema_version"
        case hits
        case indexStateRaw = "index_state"
    }
}

/// `transcript.search` response (coverage: best_effort).
struct TranscriptSearchResponse: Decodable, Sendable {
    let ok: Bool
    let schemaVersion: Int
    let daemonVersion: String
    let apiSchemaVersion: Int
    let hits: [TranscriptHit]
    private let coverageRaw: String?

    var coverage: SearchCoverage { SearchCoverage(wire: coverageRaw) }

    init(
        ok: Bool = true, schemaVersion: Int = 1, daemonVersion: String = "test",
        apiSchemaVersion: Int = 1, hits: [TranscriptHit], coverage: SearchCoverage
    ) {
        self.ok = ok
        self.schemaVersion = schemaVersion
        self.daemonVersion = daemonVersion
        self.apiSchemaVersion = apiSchemaVersion
        self.hits = hits
        self.coverageRaw = coverage.rawValue
    }

    enum CodingKeys: String, CodingKey {
        case ok
        case schemaVersion = "schema_version"
        case daemonVersion = "daemon_version"
        case apiSchemaVersion = "api_schema_version"
        case hits
        case coverageRaw = "coverage"
    }
}

/// `timeline.query` response (coverage: authoritative).
struct TimelineQueryResponse: Decodable, Sendable {
    let ok: Bool
    let schemaVersion: Int
    let daemonVersion: String
    let apiSchemaVersion: Int
    let rows: [TimelineRow]
    private let coverageRaw: String?

    var coverage: SearchCoverage { SearchCoverage(wire: coverageRaw) }

    init(
        ok: Bool = true, schemaVersion: Int = 1, daemonVersion: String = "test",
        apiSchemaVersion: Int = 1, rows: [TimelineRow], coverage: SearchCoverage
    ) {
        self.ok = ok
        self.schemaVersion = schemaVersion
        self.daemonVersion = daemonVersion
        self.apiSchemaVersion = apiSchemaVersion
        self.rows = rows
        self.coverageRaw = coverage.rawValue
    }

    enum CodingKeys: String, CodingKey {
        case ok
        case schemaVersion = "schema_version"
        case daemonVersion = "daemon_version"
        case apiSchemaVersion = "api_schema_version"
        case rows
        case coverageRaw = "coverage"
    }
}

/// `apps.list` response (SCR-179). Vocabulary source for the query parser:
/// distinct app names / bundle ids + bare `browser_url` hostnames (never a full
/// URL). A same-EUID-only browsing-profile artifact — never persisted, synced,
/// or logged by the app (R6). `truncated` flags a partial (capped) vocabulary.
struct AppsListResponse: Decodable, Sendable {
    let ok: Bool
    let schemaVersion: Int
    let daemonVersion: String
    let apiSchemaVersion: Int
    let appNames: [String]
    let appBundles: [String]
    let hostnames: [String]
    let truncated: Bool

    init(
        ok: Bool = true, schemaVersion: Int = 1, daemonVersion: String = "test",
        apiSchemaVersion: Int = 1, appNames: [String], appBundles: [String] = [],
        hostnames: [String], truncated: Bool = false
    ) {
        self.ok = ok
        self.schemaVersion = schemaVersion
        self.daemonVersion = daemonVersion
        self.apiSchemaVersion = apiSchemaVersion
        self.appNames = appNames
        self.appBundles = appBundles
        self.hostnames = hostnames
        self.truncated = truncated
    }

    enum CodingKeys: String, CodingKey {
        case ok
        case schemaVersion = "schema_version"
        case daemonVersion = "daemon_version"
        case apiSchemaVersion = "api_schema_version"
        case appNames = "app_names"
        case appBundles = "app_bundles"
        case hostnames
        case truncated
    }
}
