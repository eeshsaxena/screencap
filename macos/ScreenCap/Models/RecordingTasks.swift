import Foundation

// U10 (local-first intelligence) — the read models for a LOCAL recording's
// named task segments. These mirror the daemon's `/v0/tasks.list` verb, which
// reads the `pipeline_task_segments` ledger table inside the recording's
// local-only `recording.db` (never uploaded — R4/R8). The named tasks were
// produced on-device by the terminal-stage segmentation (U4) — or by the
// idle-gap heuristic fallback (U7) — so nothing left the Mac to obtain them.
//
// Read contract (the U4→U10 seam): the app already reads recording *rows* from
// the daemon (`/v0/recording.list`), so the per-recording task breakdown is read
// the same way — a validated, read-only daemon verb — rather than reaching into
// `recording.db` from Swift. The verb returns the STRUCTURED ledger rows (not the
// free-text `tasks.json` blob), so no re-parse is needed. A recording with no
// tasks store (provider miss, heuristic produced none, legacy recording, or a
// cloud/both recording whose local-only store is absent) returns an empty
// `tasks` list — never an error — so the UI renders gracefully.

/// One named task segment for a recording. `startTs` / `endTs` are Unix seconds
/// (the ledger's native units). `category` / `confidence` are optional provider
/// metadata; the idle-gap heuristic fallback emits neither.
struct RecordingTask: Decodable, Sendable, Hashable, Identifiable {
    let taskIndex: Int
    let startTs: Double
    let endTs: Double
    let name: String
    let category: String?
    let confidence: String?

    /// Stable within one recording — `task_index` is unique per recording by the
    /// ledger's `UNIQUE (recording_id, task_index)` constraint.
    var id: Int { taskIndex }

    init(
        taskIndex: Int,
        startTs: Double,
        endTs: Double,
        name: String,
        category: String? = nil,
        confidence: String? = nil
    ) {
        self.taskIndex = taskIndex
        self.startTs = startTs
        self.endTs = endTs
        self.name = name
        self.category = category
        self.confidence = confidence
    }

    enum CodingKeys: String, CodingKey {
        case taskIndex = "task_index"
        case startTs = "start_ts"
        case endTs = "end_ts"
        case name
        case category
        case confidence
    }
}

/// `tasks.list` input: the recording whose named tasks to read.
struct TasksListRequest: Encodable, Sendable {
    let recording: String

    init(recording: String) {
        self.recording = recording
    }
}

/// `tasks.list` response. `tasks` is empty (never absent) for a recording with
/// no tasks store — a graceful, non-error empty case.
struct TasksListResponse: Decodable, Sendable {
    let ok: Bool
    let schemaVersion: Int
    let daemonVersion: String
    let apiSchemaVersion: Int
    let recording: String
    let tasks: [RecordingTask]

    init(
        ok: Bool = true,
        schemaVersion: Int = 1,
        daemonVersion: String = "test",
        apiSchemaVersion: Int = 1,
        recording: String,
        tasks: [RecordingTask]
    ) {
        self.ok = ok
        self.schemaVersion = schemaVersion
        self.daemonVersion = daemonVersion
        self.apiSchemaVersion = apiSchemaVersion
        self.recording = recording
        self.tasks = tasks
    }

    enum CodingKeys: String, CodingKey {
        case ok
        case schemaVersion = "schema_version"
        case daemonVersion = "daemon_version"
        case apiSchemaVersion = "api_schema_version"
        case recording
        case tasks
    }
}
