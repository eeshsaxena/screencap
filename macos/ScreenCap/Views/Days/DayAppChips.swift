import Foundation

// Resolves a day's app chip: the dominant app from a single cached
// `timeline.query` per recording. Failure is silent by contract — the chip is
// simply omitted (the field is nullable end to end), so a daemon hiccup never
// degrades the Days surface into an error state.

@MainActor
final class DayAppChips: ObservableObject {
    /// Successful resolutions only, keyed by recording directory name.
    @Published private(set) var apps: [String: String] = [:]
    /// Every recording we've already queried (hit, miss, or failure) — pins the
    /// one-query-per-recording contract; a failed lookup is not retried until
    /// the view is rebuilt.
    private var attempted: Set<String> = []

    private let service: SearchService

    init(service: SearchService = LiveSearchService()) {
        self.service = service
    }

    func app(for recording: RecordingSummary) -> String? {
        apps[recording.name]
    }

    /// Resolve (at most once) the dominant app for `recording`. Cheap: one
    /// bounded `timeline.query` scoped to the recording; an explicit `limit` is
    /// required — the verb defaults to 50 and truncates earliest-first.
    func resolve(_ recording: RecordingSummary) async {
        guard !attempted.contains(recording.name) else { return }
        attempted.insert(recording.name)
        let request = TimelineQueryRequest(recording: recording.name, limit: 500)
        guard let response = try? await service.timelineQuery(request) else { return }
        if let app = DaysModel.dominantApp(response.rows) {
            apps[recording.name] = app
        }
    }
}
