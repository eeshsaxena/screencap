import Foundation

// U10 (local-first intelligence) — resolves each Journal card's named-task
// breakdown from a single cached `tasks.list` per recording. Mirrors
// `JournalAppChips` (the established one-query-per-recording resolver pattern):
// failure is silent by contract — a recording with no tasks (or a daemon hiccup)
// simply renders no task breakdown, never an error state, so the locally-derived
// tasks flow into the SAME Journal surface as any future cloud-derived tasks with
// no cloud round-trip.

@MainActor
final class JournalTasks: ObservableObject {
    /// Resolved task lists, keyed by recording directory name. Only recordings
    /// that actually have tasks appear here (an empty result stores nothing, so
    /// `tasks(for:)` returns `[]`).
    @Published private(set) var tasksByRecording: [String: [RecordingTask]] = [:]
    /// Every recording we've already queried (hit, miss, or failure) — pins the
    /// one-query-per-recording contract; a failed lookup is not retried until the
    /// view is rebuilt.
    private var attempted: Set<String> = []

    private let service: SearchService

    init(service: SearchService = LiveSearchService()) {
        self.service = service
    }

    /// The named tasks for `recording`, ordered by task index. `[]` when the
    /// recording has no tasks store (provider miss / heuristic produced none /
    /// legacy / not-yet-resolved) — the caller renders the empty case gracefully.
    func tasks(for recording: RecordingSummary) -> [RecordingTask] {
        tasksByRecording[recording.name] ?? []
    }

    /// Resolve (at most once) the named tasks for `recording`. Cheap: one
    /// read-only `tasks.list` scoped to the recording. A throw (incl. an older
    /// daemon's 404) or an empty result both leave `tasks(for:)` returning `[]`.
    func resolve(_ recording: RecordingSummary) async {
        guard !attempted.contains(recording.name) else { return }
        attempted.insert(recording.name)
        let request = TasksListRequest(recording: recording.name)
        guard let response = try? await service.tasksList(request) else { return }
        if !response.tasks.isEmpty {
            tasksByRecording[recording.name] = response.tasks
        }
    }
}
