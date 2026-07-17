import Foundation

// U10 (local-first intelligence) — resolves each Journal card's named-task
// breakdown from a single cached `tasks.list` per recording. Mirrors
// `JournalAppChips` (the established one-query-per-recording resolver pattern):
// READ failure is silent by contract — a recording with no tasks (or a daemon
// hiccup) simply renders no task breakdown, never an error state.
//
// SCR-214 U11 extends the resolver into a write-through curation layer over the
// five task CRUD verbs (`tasks.create/update/delete/merge/split`). A write
// applies an optimistic local change, calls the verb, then re-reads the
// recording's tasks so the cache converges on the daemon's authoritative index
// allocation (the store re-homes agent rows and allocates HIGH user indices —
// KTD3 — so a client can't guess the resulting index). Unlike the read path a
// FAILED write is NOT swallowed: local optimistic state reverts and a visible
// `writeError` surfaces with a retryable `pending` op (R8), so the day never
// silently diverges from the store.

@MainActor
final class JournalTasks: ObservableObject {
    /// Resolved task lists, keyed by recording directory name. Only recordings
    /// that actually have tasks appear here (an empty result stores nothing, so
    /// `tasks(for:)` returns `[]`).
    @Published private(set) var tasksByRecording: [String: [RecordingTask]] = [:]
    /// The per-recording honest-status outcome reason (U2/U3), keyed by recording name.
    @Published private(set) var reasonByRecording: [String: String] = [:]
    /// The per-recording degradation-reason detail (SCR-275 U6/U7) recorded alongside
    /// the outcome reason — e.g. `context-window` ("session too long for the on-device
    /// model"), keyed by recording name. Absent for non-degraded outcomes, legacy
    /// rows, and older daemons.
    @Published private(set) var detailByRecording: [String: String] = [:]
    /// A surfaced write failure (R8): the last CRUD verb that threw. Non-nil
    /// drives a visible retry/error affordance; the optimistic state has already
    /// been reverted, so the day still reflects the store. `Identifiable` so a
    /// SwiftUI `.alert(item:)` / `.sheet(item:)` can present it.
    @Published private(set) var writeError: TaskWriteError?
    /// Every recording we've already queried (hit, miss, or failure) — pins the
    /// one-query-per-recording contract; a failed lookup is not retried until the
    /// view is rebuilt. A successful write refreshes the cache but keeps the
    /// recording marked attempted (the refresh IS the re-query).
    private var attempted: Set<String> = []
    /// The failed write, retained so `retryLastWrite()` can replay it without the
    /// view re-deriving the arguments. Cleared on success or dismissal.
    private var pending: PendingWrite?

    private let service: SearchService

    init(service: SearchService = LiveSearchService()) {
        self.service = service
    }

    /// A surfaced, human-readable write failure. `id` makes it presentable via
    /// `.alert(item:)`; `message` is the reason to show; `isRetryable` gates the
    /// Retry affordance (always true today — every CRUD verb is replayable).
    struct TaskWriteError: Identifiable, Equatable {
        let id = UUID()
        let message: String
        var isRetryable: Bool = true
    }

    /// The replayable description of a failed write. Mirrors the five verbs so a
    /// retry re-issues the exact same call the user attempted.
    private enum PendingWrite {
        case create(recording: String, name: String, startTs: Double, endTs: Double, category: String?)
        case update(recording: String, taskIndex: Int, name: String?, startTs: Double?, endTs: Double?, category: String?)
        case delete(recording: String, taskIndex: Int)
        case merge(recording: String, taskIndices: [Int], name: String, category: String?)
        case split(recording: String, taskIndex: Int, splitTs: Double, nameLeft: String?, nameRight: String?)
    }

    // MARK: - Read

    /// The named tasks for `recording`, ordered by task index. `[]` when the
    /// recording has no tasks store (provider miss / heuristic produced none /
    /// legacy / not-yet-resolved) — the caller renders the empty case gracefully.
    func tasks(for recording: RecordingSummary) -> [RecordingTask] {
        tasksByRecording[recording.name] ?? []
    }

    /// The daemon's per-recording segmentation outcome reason (U2/U3), or `nil` when
    /// none was recorded (legacy recording / older daemon) — the card resolves that to
    /// the neutral "unknown" state (KTD6), never a false "not set up".
    func reason(for recording: RecordingSummary) -> String? {
        reasonByRecording[recording.name]
    }

    /// The distinct degradation detail recorded alongside `reason(for:)` (SCR-275
    /// U6/U7) — e.g. `context-window` ("session too long for the on-device model") vs
    /// `respond-failed`. `nil` when none was recorded (non-degraded outcome / legacy
    /// recording / older daemon); the card then renders the generic degraded copy.
    func detail(for recording: RecordingSummary) -> String? {
        detailByRecording[recording.name]
    }

    /// Whether `recording` has been queried at least once. The card gates its
    /// "unsplit — still searchable" empty-state placeholder on this so the
    /// placeholder shows only after a confirmed empty result, not during the
    /// brief pre-resolve window (avoids a flash before agent tasks land).
    func hasResolved(_ recording: RecordingSummary) -> Bool {
        attempted.contains(recording.name)
    }

    /// Resolve (at most once) the named tasks for `recording`. Cheap: one
    /// read-only `tasks.list` scoped to the recording. A throw (incl. an older
    /// daemon's 404) or an empty result both leave `tasks(for:)` returning `[]`.
    func resolve(_ recording: RecordingSummary) async {
        guard !attempted.contains(recording.name) else { return }
        await refresh(recording.name)
    }

    /// Re-read a recording's tasks unconditionally and reconcile the cache. Used
    /// after every successful write so the local view converges on the store's
    /// authoritative rows (indices, re-homing, HIGH-range allocation — KTD3).
    /// Fail-silent like `resolve`: a hiccup leaves the last-known cache intact.
    func refresh(_ recording: String) async {
        attempted.insert(recording)
        guard let response = try? await service.tasksList(TasksListRequest(recording: recording)) else { return }
        if response.tasks.isEmpty {
            tasksByRecording[recording] = nil
        } else {
            tasksByRecording[recording] = response.tasks
        }
        // U2/U3 honest status: the per-recording outcome reason (nil for a legacy
        // recording / older daemon → the card renders the neutral "unknown" state),
        // plus the SCR-275 U6/U7 degradation detail (nil assignment clears a stale
        // entry, so an upgraded outcome drops its old detail).
        reasonByRecording[recording] = response.reason
        detailByRecording[recording] = response.detail
    }

    // MARK: - Write-through (R8)

    /// Dismiss the surfaced write error and drop the retryable op (the user chose
    /// not to retry). Idempotent.
    func dismissWriteError() {
        writeError = nil
        pending = nil
    }

    /// Replay the last failed write. Clears the current error first so a second
    /// failure surfaces a fresh one. No-op when nothing is pending.
    func retryLastWrite() async {
        guard let op = pending else { return }
        pending = nil
        writeError = nil
        switch op {
        case let .create(recording, name, startTs, endTs, category):
            _ = await create(recording: recording, name: name, startTs: startTs, endTs: endTs, category: category)
        case let .update(recording, taskIndex, name, startTs, endTs, category):
            _ = await update(recording: recording, taskIndex: taskIndex, name: name, startTs: startTs, endTs: endTs, category: category)
        case let .delete(recording, taskIndex):
            _ = await delete(recording: recording, taskIndex: taskIndex)
        case let .merge(recording, taskIndices, name, category):
            _ = await merge(recording: recording, taskIndices: taskIndices, name: name, category: category)
        case let .split(recording, taskIndex, splitTs, nameLeft, nameRight):
            _ = await split(recording: recording, taskIndex: taskIndex, splitTs: splitTs, nameLeft: nameLeft, nameRight: nameRight)
        }
    }

    /// Create a USER-authored task span (AE3 — labeling unsplit footage). Returns
    /// true on success. Optimistically appends a provisional row, then refreshes
    /// so the store-allocated HIGH index replaces it.
    @discardableResult
    func create(
        recording: String,
        name: String,
        startTs: Double,
        endTs: Double,
        category: String? = nil
    ) async -> Bool {
        await writeThrough(
            recording: recording,
            optimistic: { current in
                var next = current
                next.append(
                    RecordingTask(
                        taskIndex: Self.provisionalIndex(after: current),
                        startTs: startTs, endTs: endTs, name: name, category: category
                    )
                )
                return next.sorted { $0.startTs < $1.startTs }
            },
            perform: { try await self.service.tasksCreate(
                TasksCreateRequest(recording: recording, name: name, startTs: startTs, endTs: endTs, category: category)
            ) },
            describe: "Couldn't add the task",
            pending: .create(recording: recording, name: name, startTs: startTs, endTs: endTs, category: category)
        )
    }

    /// Rename and/or re-bound an existing task. Editing an agent task routes here
    /// too — the daemon marks it `edited=1` so the next agent pass preserves it.
    @discardableResult
    func update(
        recording: String,
        taskIndex: Int,
        name: String? = nil,
        startTs: Double? = nil,
        endTs: Double? = nil,
        category: String? = nil
    ) async -> Bool {
        await writeThrough(
            recording: recording,
            optimistic: { current in
                current.map { task in
                    guard task.taskIndex == taskIndex else { return task }
                    return RecordingTask(
                        taskIndex: task.taskIndex,
                        startTs: startTs ?? task.startTs,
                        endTs: endTs ?? task.endTs,
                        name: name ?? task.name,
                        category: category ?? task.category,
                        confidence: task.confidence
                    )
                }
            },
            perform: { try await self.service.tasksUpdate(
                TasksUpdateRequest(recording: recording, taskIndex: taskIndex, name: name, startTs: startTs, endTs: endTs, category: category)
            ) },
            describe: "Couldn't save the change",
            pending: .update(recording: recording, taskIndex: taskIndex, name: name, startTs: startTs, endTs: endTs, category: category)
        )
    }

    /// Convenience rename (name-only update).
    @discardableResult
    func rename(recording: String, taskIndex: Int, to name: String) async -> Bool {
        await update(recording: recording, taskIndex: taskIndex, name: name)
    }

    /// Delete one task by index.
    @discardableResult
    func delete(recording: String, taskIndex: Int) async -> Bool {
        await writeThrough(
            recording: recording,
            optimistic: { $0.filter { $0.taskIndex != taskIndex } },
            perform: { try await self.service.tasksDelete(
                TasksDeleteRequest(recording: recording, taskIndex: taskIndex)
            ) },
            describe: "Couldn't delete the task",
            pending: .delete(recording: recording, taskIndex: taskIndex)
        )
    }

    /// Merge >=2 segments into one labeled span.
    @discardableResult
    func merge(recording: String, taskIndices: [Int], name: String, category: String? = nil) async -> Bool {
        let merged = Set(taskIndices)
        return await writeThrough(
            recording: recording,
            optimistic: { current in
                let kept = current.filter { !merged.contains($0.taskIndex) }
                let victims = current.filter { merged.contains($0.taskIndex) }
                guard let lo = victims.map(\.startTs).min(), let hi = victims.map(\.endTs).max() else { return current }
                var next = kept
                next.append(
                    RecordingTask(taskIndex: Self.provisionalIndex(after: current), startTs: lo, endTs: hi, name: name, category: category)
                )
                return next.sorted { $0.startTs < $1.startTs }
            },
            perform: { try await self.service.tasksMerge(
                TasksMergeRequest(recording: recording, taskIndices: taskIndices, name: name, category: category)
            ) },
            describe: "Couldn't merge the tasks",
            pending: .merge(recording: recording, taskIndices: taskIndices, name: name, category: category)
        )
    }

    /// Split one segment into two at `splitTs` (Unix seconds).
    @discardableResult
    func split(
        recording: String,
        taskIndex: Int,
        splitTs: Double,
        nameLeft: String? = nil,
        nameRight: String? = nil
    ) async -> Bool {
        await writeThrough(
            recording: recording,
            optimistic: { current in
                guard let target = current.first(where: { $0.taskIndex == taskIndex }),
                      splitTs > target.startTs, splitTs < target.endTs else { return current }
                var next = current.filter { $0.taskIndex != taskIndex }
                let base = Self.provisionalIndex(after: current)
                next.append(RecordingTask(taskIndex: base, startTs: target.startTs, endTs: splitTs, name: nameLeft ?? target.name, category: target.category))
                next.append(RecordingTask(taskIndex: base + 1, startTs: splitTs, endTs: target.endTs, name: nameRight ?? target.name, category: target.category))
                return next.sorted { $0.startTs < $1.startTs }
            },
            perform: { try await self.service.tasksSplit(
                TasksSplitRequest(recording: recording, taskIndex: taskIndex, splitTs: splitTs, nameLeft: nameLeft, nameRight: nameRight)
            ) },
            describe: "Couldn't split the task",
            pending: .split(recording: recording, taskIndex: taskIndex, splitTs: splitTs, nameLeft: nameLeft, nameRight: nameRight)
        )
    }

    // MARK: - Write-through core

    /// One optimistic-apply → verb → refresh (success) / revert (failure) cycle.
    /// The verb's concrete response is discarded — `refresh` re-reads the store
    /// as the single source of truth, so a client never guesses the resulting
    /// index. On any throw the pre-write snapshot is restored and `writeError`
    /// surfaces (R8: visible failure + no silent divergence).
    private func writeThrough<Response>(
        recording: String,
        optimistic: ([RecordingTask]) -> [RecordingTask],
        perform: @escaping () async throws -> Response,
        describe: String,
        pending pendingOp: PendingWrite
    ) async -> Bool {
        let snapshot = tasksByRecording[recording]
        let optimisticTasks = optimistic(snapshot ?? [])
        tasksByRecording[recording] = optimisticTasks.isEmpty ? nil : optimisticTasks
        do {
            _ = try await perform()
            writeError = nil
            pending = nil
            await refresh(recording)
            return true
        } catch {
            // Revert to the exact pre-write cache and surface a retryable error.
            tasksByRecording[recording] = snapshot
            pending = pendingOp
            writeError = TaskWriteError(message: Self.describe(error, fallback: describe))
            return false
        }
    }

    /// A provisional (never persisted) index for an optimistic row — above any
    /// current index so it can't collide before `refresh` replaces it.
    private static func provisionalIndex(after tasks: [RecordingTask]) -> Int {
        (tasks.map(\.taskIndex).max() ?? -1) + 1_000
    }

    /// Human-readable reason for a surfaced write failure. A typed daemon
    /// envelope maps to plain copy; anything else falls back to the op label.
    private static func describe(_ error: Error, fallback: String) -> String {
        if case let DaemonClientError.envelopeError(code, _) = error {
            switch code {
            case "invalid_request":
                return "\(fallback): that time span isn't valid for this recording."
            case "invalid_name":
                return "\(fallback): the recording name is invalid."
            case "recording_not_found":
                return "\(fallback): this recording is no longer available."
            default:
                return "\(fallback) (\(code))."
            }
        }
        return "\(fallback). \(error.localizedDescription)"
    }
}
