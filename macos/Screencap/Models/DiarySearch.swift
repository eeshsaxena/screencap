import Foundation

// U8 (day diary, R5/KTD-8) — the Swift READ models for the FREE-tier
// `/v0/diary.search` verb (U6). Mirror the daemon's `DiarySearchRequest` /
// `DiaryBlockHit` / `DiarySearchResponse` (src/screencap/daemon/schema.py).
//
// Diary search is the FREE-tier "find a weeks-old moment" surface (KTD-8): it
// ranks day-diary BLOCK snippets (name + topic bullets) over the `diary_fts`
// table inside the LOCAL content index — never the paid Recall palette. By shape
// each hit is POINTER ONLY: a text `snippet` + the `(recording, block_id)`
// pointer + the block's absolute unix-ms span, never a media path, image bytes,
// or a full bullet dump. The app deep-links by `blockId` and locates the block
// in its day by span (R5, enforced at the daemon boundary). The NARRATIVE is
// never indexed.

/// `diary.search` input: a query with an OPTIONAL recording filter (search every
/// recording when absent) — the free-tier Tasks history search reach beyond the
/// loaded window. `limit` bounds the returned hits.
struct DiarySearchRequest: Encodable, Sendable {
    let query: String
    let recording: String?
    let limit: Int?

    init(query: String, recording: String? = nil, limit: Int? = nil) {
        self.query = query
        self.recording = recording
        self.limit = limit
    }
}

/// A single day-diary block match — POINTER ONLY (R5/KTD-8). `startMs` / `endMs`
/// are the block's absolute unix-ms span; the app deep-links by `blockId` and
/// locates the block in its day by span. It never receives bullet text beyond the
/// matched `snippet`.
struct DiaryBlockHit: Decodable, Sendable, Hashable, Identifiable {
    let recording: String
    let blockId: String
    let startMs: Int
    let endMs: Int
    let snippet: String
    let score: Double

    /// Stable within a result set — `(recording, block_id)` is the block's
    /// cross-recording-unique pointer.
    var id: String { "\(recording)#\(blockId)" }

    init(
        recording: String,
        blockId: String,
        startMs: Int,
        endMs: Int,
        snippet: String,
        score: Double = 0
    ) {
        self.recording = recording
        self.blockId = blockId
        self.startMs = startMs
        self.endMs = endMs
        self.snippet = snippet
        self.score = score
    }

    enum CodingKeys: String, CodingKey {
        case recording
        case blockId = "block_id"
        case startMs = "start_ms"
        case endMs = "end_ms"
        case snippet
        case score
    }
}

/// `diary.search` response. `indexState` applies the same pessimistic defaulting
/// as `content.search` (an unknown / missing value → `.storeUnavailable`, never
/// silently "ok"). `storeState` carries the vault state (KTD-14): a locked /
/// absent / error store returns empty hits + a degraded state. Absent on an older
/// daemon → `"mounted"`.
struct DiarySearchResponse: Decodable, Sendable {
    let ok: Bool
    let schemaVersion: Int
    let daemonVersion: String
    let apiSchemaVersion: Int
    let hits: [DiaryBlockHit]
    private let indexStateRaw: String?
    let storeState: String?

    var indexState: ContentIndexState { ContentIndexState(wire: indexStateRaw) }

    /// The typed store state, tolerant of an older daemon that omits the field.
    var resolvedStoreState: StoreState { StoreState.from(state: storeState) }

    init(
        ok: Bool = true,
        schemaVersion: Int = 1,
        daemonVersion: String = "test",
        apiSchemaVersion: Int = 1,
        hits: [DiaryBlockHit],
        indexState: ContentIndexState,
        storeState: String? = "mounted"
    ) {
        self.ok = ok
        self.schemaVersion = schemaVersion
        self.daemonVersion = daemonVersion
        self.apiSchemaVersion = apiSchemaVersion
        self.hits = hits
        self.indexStateRaw = indexState.rawValue
        self.storeState = storeState
    }

    enum CodingKeys: String, CodingKey {
        case ok
        case schemaVersion = "schema_version"
        case daemonVersion = "daemon_version"
        case apiSchemaVersion = "api_schema_version"
        case hits
        case indexStateRaw = "index_state"
        case storeState = "store_state"
    }
}
