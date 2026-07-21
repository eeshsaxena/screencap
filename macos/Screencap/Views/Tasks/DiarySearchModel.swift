import Foundation

// U8 (day diary, R5/KTD-8) — the FREE-tier diary-search view-model behind the
// Tasks surface's search field. The in-app substring filter (`TasksModel.filter`)
// only reaches the LOADED window; this model queries the `diary.search` verb to
// reach the user's WHOLE history ("find a weeks-old moment in under a minute"),
// still free-tier and local-only — the paid Recall palette is never involved.
//
// Fail-open by contract (mirrors `DayTasks`): a daemon miss / older-daemon 404 /
// sealed vault degrades to NO history results (the local filter still covers the
// loaded rows), never an error state. Injected with a `SearchService` so the
// mapping + gating are unit-testable against a fake (the established seam).

@MainActor
final class DiarySearchModel: ObservableObject {
    /// The mapped history results for the current query (daemon rank order), or
    /// empty when the query is blank, still in flight, or nothing matched.
    @Published private(set) var results: [TasksModel.DiaryResultRow] = []
    /// True while a query is in flight — drives a quiet inline spinner, never a
    /// full-surface loader (the loaded-row filter stays visible meanwhile).
    @Published private(set) var searching = false
    /// The query the current `results` correspond to, so a stale in-flight result
    /// for an older query is dropped rather than shown against a newer one.
    private var activeQuery = ""

    private let service: SearchService

    init(service: SearchService = LiveSearchService()) {
        self.service = service
    }

    /// Bound so a single request can't drive an unbounded UI list; the daemon
    /// caps server-side too.
    private static let resultLimit = 50

    /// Run a history diary search for `query`. A blank query clears results
    /// without hitting the daemon. Fail-open: any throw leaves results empty and
    /// clears the spinner — the caller's local filter continues to work.
    func search(_ query: String, now: Date = Date()) async {
        let needle = query.trimmingCharacters(in: .whitespacesAndNewlines)
        activeQuery = needle
        guard !needle.isEmpty else {
            results = []
            searching = false
            return
        }
        searching = true
        defer { searching = false }
        guard let response = try? await service.diarySearch(
            DiarySearchRequest(query: needle, limit: Self.resultLimit)
        ) else {
            // Daemon down / older daemon / sealed vault → no history results, but
            // the loaded-row filter still applies. Only apply if still the active
            // query (a newer keystroke may have superseded this one).
            if activeQuery == needle { results = [] }
            return
        }
        // Drop a stale response whose query the user has already moved past.
        guard activeQuery == needle else { return }
        results = TasksModel.diaryResultRows(from: response.hits, now: now)
    }

    /// Clear results (query emptied / surface dismissed).
    func clear() {
        activeQuery = ""
        results = []
        searching = false
    }
}
