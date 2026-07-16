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
    /// U2/U3 (honest status): why this recording has (or lacks) AI-named tasks —
    /// `produced_tasks` / `mechanical_only` / `nothing_to_name` / `couldnt_run` /
    /// `in_progress`. `nil` for a legacy recording with no recorded outcome, or an
    /// older daemon that omits the field — both render as the neutral "unknown"
    /// state (KTD6), never a false "not set up".
    let reason: String?

    init(
        ok: Bool = true,
        schemaVersion: Int = 1,
        daemonVersion: String = "test",
        apiSchemaVersion: Int = 1,
        recording: String,
        tasks: [RecordingTask],
        reason: String? = nil
    ) {
        self.ok = ok
        self.schemaVersion = schemaVersion
        self.daemonVersion = daemonVersion
        self.apiSchemaVersion = apiSchemaVersion
        self.recording = recording
        self.tasks = tasks
        self.reason = reason
    }

    enum CodingKeys: String, CodingKey {
        case ok
        case schemaVersion = "schema_version"
        case daemonVersion = "daemon_version"
        case apiSchemaVersion = "api_schema_version"
        case recording
        case tasks
        case reason
    }
}

// MARK: - SCR-214 U7 task CRUD write verbs (request/response models)
//
// The five mutating verbs mirror `recording.rename`'s post-hoc, LOCAL-only
// pattern — the rows live in `pipeline_task_segments` inside the recording's
// `recording.db` and are never uploaded (R4/R8). `source` / `edited` (KTD3) are
// deliberately NOT on the wire: task ownership is an internal store concern, so
// the app never sends or decodes them. Editing an agent task simply routes
// through `tasks.update`, and the daemon marks the row `edited=1` server-side so
// the next agent re-segmentation preserves it.
//
// Daemon error codes surface through the existing `DaemonClientError.envelopeError`
// seam unchanged: `invalid_name` (traversal recording name, 400), `invalid_request`
// (bad/zero-length/out-of-range/evicted-footage span, 400), and
// `recording_not_found` (404). Optional request fields are `Optional` so the
// synthesized `Encodable` omits them (via `encodeIfPresent`) when nil, mirroring
// `RecordingStartRequest.audio`.

/// `tasks.create` input (U7): add a USER-authored task span. `startTs`/`endTs`
/// are Unix seconds. The daemon forces `source='user'` at a disjoint HIGH
/// `task_index` (KTD3) — the caller never chooses the index.
struct TasksCreateRequest: Encodable, Sendable {
    let recording: String
    let name: String
    let startTs: Double
    let endTs: Double
    let category: String?

    init(recording: String, name: String, startTs: Double, endTs: Double, category: String? = nil) {
        self.recording = recording
        self.name = name
        self.startTs = startTs
        self.endTs = endTs
        self.category = category
    }

    enum CodingKeys: String, CodingKey {
        case recording
        case name
        case startTs = "start_ts"
        case endTs = "end_ts"
        case category
    }
}

/// `tasks.create` output: the created row echoed as a full `RecordingTask`
/// (same wire shape as `tasks.list`), carrying its store-allocated HIGH
/// `task_index` so the app can address it later without a re-list.
struct TasksCreateResponse: Decodable, Sendable {
    let ok: Bool
    let schemaVersion: Int
    let daemonVersion: String
    let apiSchemaVersion: Int
    let recording: String
    let task: RecordingTask

    init(
        ok: Bool = true,
        schemaVersion: Int = 1,
        daemonVersion: String = "test",
        apiSchemaVersion: Int = 1,
        recording: String,
        task: RecordingTask
    ) {
        self.ok = ok
        self.schemaVersion = schemaVersion
        self.daemonVersion = daemonVersion
        self.apiSchemaVersion = apiSchemaVersion
        self.recording = recording
        self.task = task
    }

    enum CodingKeys: String, CodingKey {
        case ok
        case schemaVersion = "schema_version"
        case daemonVersion = "daemon_version"
        case apiSchemaVersion = "api_schema_version"
        case recording
        case task
    }
}

/// `tasks.update` input (U7): rename / re-bound an existing task by `taskIndex`.
/// Only the provided fields are written; every edit marks the row curated
/// (`edited=1`) and re-homes an agent row into the HIGH range so the next agent
/// replace preserves it (KTD3).
struct TasksUpdateRequest: Encodable, Sendable {
    let recording: String
    let taskIndex: Int
    let name: String?
    let startTs: Double?
    let endTs: Double?
    let category: String?

    init(
        recording: String,
        taskIndex: Int,
        name: String? = nil,
        startTs: Double? = nil,
        endTs: Double? = nil,
        category: String? = nil
    ) {
        self.recording = recording
        self.taskIndex = taskIndex
        self.name = name
        self.startTs = startTs
        self.endTs = endTs
        self.category = category
    }

    enum CodingKeys: String, CodingKey {
        case recording
        case taskIndex = "task_index"
        case name
        case startTs = "start_ts"
        case endTs = "end_ts"
        case category
    }
}

/// `tasks.update` output: the row's (possibly re-homed) `task_index`.
struct TasksUpdateResponse: Decodable, Sendable {
    let ok: Bool
    let schemaVersion: Int
    let daemonVersion: String
    let apiSchemaVersion: Int
    let recording: String
    let taskIndex: Int

    init(
        ok: Bool = true,
        schemaVersion: Int = 1,
        daemonVersion: String = "test",
        apiSchemaVersion: Int = 1,
        recording: String,
        taskIndex: Int
    ) {
        self.ok = ok
        self.schemaVersion = schemaVersion
        self.daemonVersion = daemonVersion
        self.apiSchemaVersion = apiSchemaVersion
        self.recording = recording
        self.taskIndex = taskIndex
    }

    enum CodingKeys: String, CodingKey {
        case ok
        case schemaVersion = "schema_version"
        case daemonVersion = "daemon_version"
        case apiSchemaVersion = "api_schema_version"
        case recording
        case taskIndex = "task_index"
    }
}

/// `tasks.delete` input (U7): remove one task by `taskIndex`.
struct TasksDeleteRequest: Encodable, Sendable {
    let recording: String
    let taskIndex: Int

    init(recording: String, taskIndex: Int) {
        self.recording = recording
        self.taskIndex = taskIndex
    }

    enum CodingKeys: String, CodingKey {
        case recording
        case taskIndex = "task_index"
    }
}

/// `tasks.delete` output: `deleted` is true when a row went, false when none
/// matched (idempotent — a dropped/retried delete converges, never an error).
struct TasksDeleteResponse: Decodable, Sendable {
    let ok: Bool
    let schemaVersion: Int
    let daemonVersion: String
    let apiSchemaVersion: Int
    let recording: String
    let deleted: Bool

    init(
        ok: Bool = true,
        schemaVersion: Int = 1,
        daemonVersion: String = "test",
        apiSchemaVersion: Int = 1,
        recording: String,
        deleted: Bool
    ) {
        self.ok = ok
        self.schemaVersion = schemaVersion
        self.daemonVersion = daemonVersion
        self.apiSchemaVersion = apiSchemaVersion
        self.recording = recording
        self.deleted = deleted
    }

    enum CodingKeys: String, CodingKey {
        case ok
        case schemaVersion = "schema_version"
        case daemonVersion = "daemon_version"
        case apiSchemaVersion = "api_schema_version"
        case recording
        case deleted
    }
}

/// `tasks.merge` input (U7): combine >=2 segments (`taskIndices`, >=2 distinct)
/// into one atomic `source='user'` row with `name` as the surviving label and
/// the union as its span.
struct TasksMergeRequest: Encodable, Sendable {
    let recording: String
    let taskIndices: [Int]
    let name: String
    let category: String?

    init(recording: String, taskIndices: [Int], name: String, category: String? = nil) {
        self.recording = recording
        self.taskIndices = taskIndices
        self.name = name
        self.category = category
    }

    enum CodingKeys: String, CodingKey {
        case recording
        case taskIndices = "task_indices"
        case name
        case category
    }
}

/// `tasks.merge` output: the merged row's allocated HIGH `task_index`.
struct TasksMergeResponse: Decodable, Sendable {
    let ok: Bool
    let schemaVersion: Int
    let daemonVersion: String
    let apiSchemaVersion: Int
    let recording: String
    let taskIndex: Int

    init(
        ok: Bool = true,
        schemaVersion: Int = 1,
        daemonVersion: String = "test",
        apiSchemaVersion: Int = 1,
        recording: String,
        taskIndex: Int
    ) {
        self.ok = ok
        self.schemaVersion = schemaVersion
        self.daemonVersion = daemonVersion
        self.apiSchemaVersion = apiSchemaVersion
        self.recording = recording
        self.taskIndex = taskIndex
    }

    enum CodingKeys: String, CodingKey {
        case ok
        case schemaVersion = "schema_version"
        case daemonVersion = "daemon_version"
        case apiSchemaVersion = "api_schema_version"
        case recording
        case taskIndex = "task_index"
    }
}

/// `tasks.split` input (U7): split one segment into two at `splitTs` (Unix
/// seconds, strictly within the segment's span). Optional `nameLeft`/`nameRight`
/// label the halves; each defaults to the original name.
struct TasksSplitRequest: Encodable, Sendable {
    let recording: String
    let taskIndex: Int
    let splitTs: Double
    let nameLeft: String?
    let nameRight: String?

    init(recording: String, taskIndex: Int, splitTs: Double, nameLeft: String? = nil, nameRight: String? = nil) {
        self.recording = recording
        self.taskIndex = taskIndex
        self.splitTs = splitTs
        self.nameLeft = nameLeft
        self.nameRight = nameRight
    }

    enum CodingKeys: String, CodingKey {
        case recording
        case taskIndex = "task_index"
        case splitTs = "split_ts"
        case nameLeft = "name_left"
        case nameRight = "name_right"
    }
}

/// `tasks.split` output: the two allocated `task_index` values, left-span first.
struct TasksSplitResponse: Decodable, Sendable {
    let ok: Bool
    let schemaVersion: Int
    let daemonVersion: String
    let apiSchemaVersion: Int
    let recording: String
    let taskIndices: [Int]

    init(
        ok: Bool = true,
        schemaVersion: Int = 1,
        daemonVersion: String = "test",
        apiSchemaVersion: Int = 1,
        recording: String,
        taskIndices: [Int]
    ) {
        self.ok = ok
        self.schemaVersion = schemaVersion
        self.daemonVersion = daemonVersion
        self.apiSchemaVersion = apiSchemaVersion
        self.recording = recording
        self.taskIndices = taskIndices
    }

    enum CodingKeys: String, CodingKey {
        case ok
        case schemaVersion = "schema_version"
        case daemonVersion = "daemon_version"
        case apiSchemaVersion = "api_schema_version"
        case recording
        case taskIndices = "task_indices"
    }
}
