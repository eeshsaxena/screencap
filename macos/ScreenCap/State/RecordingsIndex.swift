import Combine
import Foundation

/// Cache for `screencap list --json` output. Reloaded on init and on demand
/// (Unit 13 calls `refresh()` when the `recording_finalized` stderr event fires).
///
/// This is intentionally a state-only ObservableObject — no lifecycle, no
/// timers. Two consumers: CalendarView (heat map) and RecordingsListView
/// (the actual list).
@MainActor
final class RecordingsIndex: ObservableObject {
    @Published private(set) var recordings: [RecordingSummary] = []
    @Published private(set) var isLoading: Bool = false
    @Published private(set) var lastError: String?

    init(autoload: Bool = true) {
        if autoload {
            Task { await refresh() }
        }
    }

    var totalCount: Int { recordings.count }

    /// Map of calendar-day-start → recording count, used by CalendarView's
    /// heat map. Days with zero recordings are not present in the map.
    var countsByDay: [Date: Int] {
        var counts: [Date: Int] = [:]
        for rec in recordings {
            guard let day = rec.startedDay else { continue }
            counts[day, default: 0] += 1
        }
        return counts
    }

    /// Recordings whose `startedDay` is `day`, sorted newest-first.
    func recordings(on day: Date) -> [RecordingSummary] {
        let target = Calendar.current.startOfDay(for: day)
        return recordings
            .filter { $0.startedDay == target }
            .sorted { ($0.startedAt ?? 0) > ($1.startedAt ?? 0) }
    }

    /// Recordings grouped by calendar-day-start, sorted newest-day-first then
    /// newest-recording-first within each day.
    func groupedByDay() -> [(day: Date, recordings: [RecordingSummary])] {
        let groups = Dictionary(grouping: recordings) { $0.startedDay ?? .distantPast }
        return groups
            .map { (day: $0.key, recordings: $0.value.sorted { ($0.startedAt ?? 0) > ($1.startedAt ?? 0) }) }
            .sorted { $0.day > $1.day }
    }

    func refresh() async {
        isLoading = true
        defer { isLoading = false }
        do {
            let rows: [RecordingSummary] = try await CLIClient.runJSON(["list", "--json"])
            recordings = rows
            lastError = nil
        } catch {
            // Clear the cache even on failure so a delete-everything sweep
            // doesn't leave stale rows on the calendar / list. Older CLIs
            // (pre-fix) emitted plain prose for an empty archive instead of
            // `[]`, producing a decode error here — the right state for the
            // user is "no recordings", not "no recordings + an error toast".
            recordings = []
            lastError = error.localizedDescription
        }
    }
}
