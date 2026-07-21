import Foundation

// U6 (day-first Tasks surface) — the Swift READ models for the cross-day
// `/v0/tasks.query` verb (U5). Mirror the daemon's `TasksQueryTask` /
// `TasksQueryDay` / `TasksQueryRecordingStatus` / `TasksQueryResponse`
// (src/screencap/daemon/schema.py). Local-only, read-only (R4/R8): the recording
// key rides each task for pointer resolution + curation, but is NEVER rendered as
// a browsing entity (R5) — the Tasks surface shows the task NAME, its day, and its
// time range. `start_ts` / `end_ts` are Unix seconds (the ledger's native units).
//
// Every field an older daemon might omit is tolerant (`decodeIfPresent`), and each
// model carries a memberwise init so `TasksModelTests` can construct fixtures
// without a live socket.

/// One named task in the cross-day list, carrying its recording pointer. Reuses
/// the shared 6-field task shape (`task_index` / `start_ts` / `end_ts` / `name` /
/// `category` / `confidence`) plus the `recording` (+ `recordingId`) it came from —
/// the Tasks surface needs the recording key to seek into its day page (KTD-1) and
/// to curate the task (rename/split/merge/delete route through the per-recording
/// CRUD verbs), never to display it.
struct TasksQueryTask: Decodable, Sendable, Hashable, Identifiable {
    let recording: String
    let recordingId: String?
    let taskIndex: Int
    let startTs: Double
    let endTs: Double
    let name: String
    let category: String?
    let confidence: String?
    /// Day-diary fields (U1/KTD-9, additive) — mirror `RecordingTask` so the
    /// cross-day surface sees exactly what per-recording `tasks.list` sees.
    /// `bullets` are the block's short topic summaries; `blockId` is the stable
    /// opaque identity (deep-links resolve by it, NOT `task_index`); `threadId`
    /// links same-work blocks within a day; `isOpen` marks the live trailing block.
    let bullets: [String]
    let blockId: String?
    let threadId: String?
    let isOpen: Bool
    /// Thread rollup (U7/R7, additive): total minutes across the thread's
    /// sittings, the sitting count, and this block's 1-based place. `tasks.query`
    /// populates them for a thread of >=2; a lone block reports all three `nil`.
    let threadTotalMinutes: Double?
    let threadSittingCount: Int?
    let threadSittingIndex: Int?

    /// Stable across the loaded window — `(recording, task_index)` is unique by the
    /// ledger's `UNIQUE (recording_id, task_index)` constraint.
    var id: String { "\(recording)#\(taskIndex)" }

    init(
        recording: String,
        recordingId: String? = nil,
        taskIndex: Int,
        startTs: Double,
        endTs: Double,
        name: String,
        category: String? = nil,
        confidence: String? = nil,
        bullets: [String] = [],
        blockId: String? = nil,
        threadId: String? = nil,
        isOpen: Bool = false,
        threadTotalMinutes: Double? = nil,
        threadSittingCount: Int? = nil,
        threadSittingIndex: Int? = nil
    ) {
        self.recording = recording
        self.recordingId = recordingId
        self.taskIndex = taskIndex
        self.startTs = startTs
        self.endTs = endTs
        self.name = name
        self.category = category
        self.confidence = confidence
        self.bullets = bullets
        self.blockId = blockId
        self.threadId = threadId
        self.isOpen = isOpen
        self.threadTotalMinutes = threadTotalMinutes
        self.threadSittingCount = threadSittingCount
        self.threadSittingIndex = threadSittingIndex
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        recording = try c.decode(String.self, forKey: .recording)
        recordingId = try c.decodeIfPresent(String.self, forKey: .recordingId)
        taskIndex = try c.decode(Int.self, forKey: .taskIndex)
        startTs = try c.decode(Double.self, forKey: .startTs)
        endTs = try c.decode(Double.self, forKey: .endTs)
        name = try c.decode(String.self, forKey: .name)
        category = try c.decodeIfPresent(String.self, forKey: .category)
        confidence = try c.decodeIfPresent(String.self, forKey: .confidence)
        // Day-diary fields — additive, decoded tolerantly for forward compat.
        bullets = try c.decodeIfPresent([String].self, forKey: .bullets) ?? []
        blockId = try c.decodeIfPresent(String.self, forKey: .blockId)
        threadId = try c.decodeIfPresent(String.self, forKey: .threadId)
        isOpen = try c.decodeIfPresent(Bool.self, forKey: .isOpen) ?? false
        threadTotalMinutes = try c.decodeIfPresent(Double.self, forKey: .threadTotalMinutes)
        threadSittingCount = try c.decodeIfPresent(Int.self, forKey: .threadSittingCount)
        threadSittingIndex = try c.decodeIfPresent(Int.self, forKey: .threadSittingIndex)
    }

    enum CodingKeys: String, CodingKey {
        case recording
        case recordingId = "recording_id"
        case taskIndex = "task_index"
        case startTs = "start_ts"
        case endTs = "end_ts"
        case name
        case category
        case confidence
        case bullets
        case blockId = "block_id"
        case threadId = "thread_id"
        case isOpen = "is_open"
        case threadTotalMinutes = "thread_total_minutes"
        case threadSittingCount = "thread_sitting_count"
        case threadSittingIndex = "thread_sitting_index"
    }
}

/// All tasks mapping to one local calendar day (KTD-11), start-ordered. The
/// daemon returns `days` reverse-chronological (newest first).
struct TasksQueryDay: Decodable, Sendable, Hashable {
    let date: String
    let tasks: [TasksQueryTask]

    init(date: String, tasks: [TasksQueryTask]) {
        self.date = date
        self.tasks = tasks
    }
}

/// One recording's honest-status rollup entry (R21 zero states). Every recording
/// whose coverage intersects the range appears — even one that named no tasks — so
/// the Tasks surface can distinguish "nothing on file" from "intelligence produced
/// nothing" (`nothing_to_name` / `mechanical_only` / `couldnt_run` / `in_progress`
/// / `nil` → unknown), never a false "you did nothing". `state` is the recording's
/// pipeline state; `reason` / `detail` share the `tasks.list` outcome vocabulary
/// (resolved through `RecordingHonestState`). `name` is opaque plumbing, never
/// rendered (R5).
struct TasksQueryRecordingStatus: Decodable, Sendable, Hashable {
    let name: String
    let recordingId: String?
    let state: String
    let reason: String?
    let detail: String?

    init(
        name: String,
        recordingId: String? = nil,
        state: String = "ready",
        reason: String? = nil,
        detail: String? = nil
    ) {
        self.name = name
        self.recordingId = recordingId
        self.state = state
        self.reason = reason
        self.detail = detail
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        name = try c.decode(String.self, forKey: .name)
        recordingId = try c.decodeIfPresent(String.self, forKey: .recordingId)
        state = try c.decodeIfPresent(String.self, forKey: .state) ?? "ready"
        reason = try c.decodeIfPresent(String.self, forKey: .reason)
        detail = try c.decodeIfPresent(String.self, forKey: .detail)
    }

    enum CodingKeys: String, CodingKey {
        case name
        case recordingId = "recording_id"
        case state
        case reason
        case detail
    }
}

/// `tasks.query` response: cross-day task segments grouped by day (reverse-chron)
/// plus the per-recording honest-status rollup. `storeState` carries the vault
/// state (KTD-14): a locked / absent / error store returns empty `days` +
/// `recordings` with a degraded state (never a 500) so the surface branches to
/// `StoreStateView` before its empty check. Absent on an older daemon →
/// `"mounted"`.
struct TasksQueryResponse: Decodable, Sendable {
    let ok: Bool
    let schemaVersion: Int
    let daemonVersion: String
    let apiSchemaVersion: Int
    let startDate: String
    let endDate: String
    let days: [TasksQueryDay]
    let recordings: [TasksQueryRecordingStatus]
    let storeState: String?

    init(
        ok: Bool = true,
        schemaVersion: Int = 1,
        daemonVersion: String = "test",
        apiSchemaVersion: Int = 1,
        startDate: String,
        endDate: String,
        days: [TasksQueryDay],
        recordings: [TasksQueryRecordingStatus],
        storeState: String? = "mounted"
    ) {
        self.ok = ok
        self.schemaVersion = schemaVersion
        self.daemonVersion = daemonVersion
        self.apiSchemaVersion = apiSchemaVersion
        self.startDate = startDate
        self.endDate = endDate
        self.days = days
        self.recordings = recordings
        self.storeState = storeState
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        ok = try c.decode(Bool.self, forKey: .ok)
        schemaVersion = try c.decode(Int.self, forKey: .schemaVersion)
        daemonVersion = try c.decode(String.self, forKey: .daemonVersion)
        apiSchemaVersion = try c.decode(Int.self, forKey: .apiSchemaVersion)
        startDate = try c.decode(String.self, forKey: .startDate)
        endDate = try c.decode(String.self, forKey: .endDate)
        days = try c.decodeIfPresent([TasksQueryDay].self, forKey: .days) ?? []
        recordings = try c.decodeIfPresent([TasksQueryRecordingStatus].self, forKey: .recordings) ?? []
        storeState = try c.decodeIfPresent(String.self, forKey: .storeState)
    }

    /// The typed store state, tolerant of an older daemon that omits the field.
    var resolvedStoreState: StoreState { StoreState.from(state: storeState) }

    enum CodingKeys: String, CodingKey {
        case ok
        case schemaVersion = "schema_version"
        case daemonVersion = "daemon_version"
        case apiSchemaVersion = "api_schema_version"
        case startDate = "start_date"
        case endDate = "end_date"
        case days
        case recordings
        case storeState = "store_state"
    }
}

/// `tasks.query` input: a local calendar date RANGE (`YYYY-MM-DD`) plus the
/// caller's UTC offset so the daemon groups by the same local calendar day the UI
/// shows (KTD-11). `tzOffsetSeconds` is seconds EAST of UTC.
struct TasksQueryRequest: Encodable, Sendable {
    let startDate: String
    let endDate: String
    let tzOffsetSeconds: Int

    init(startDate: String, endDate: String, tzOffsetSeconds: Int) {
        self.startDate = startDate
        self.endDate = endDate
        self.tzOffsetSeconds = tzOffsetSeconds
    }

    enum CodingKeys: String, CodingKey {
        case startDate = "start_date"
        case endDate = "end_date"
        case tzOffsetSeconds = "tz_offset_seconds"
    }
}
