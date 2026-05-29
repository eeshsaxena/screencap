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

    private var uploadSucceededObserver: NSObjectProtocol?

    init(autoload: Bool = true) {
        if autoload {
            Task { await refresh() }
        }
        // Plan U9: refresh after a successful upload from any review window
        // so the source row's `isUploadEligible` predicate flips off
        // `uploaded` and the Upload button disappears on the next render.
        // ReviewWindow posts this notification via LiveReviewWindowEffects.
        uploadSucceededObserver = NotificationCenter.default.addObserver(
            forName: .reviewWindowUploadSucceeded,
            object: nil,
            queue: .main
        ) { [weak self] _ in
            Task { @MainActor [weak self] in
                await self?.refresh()
            }
        }
    }

    deinit {
        if let uploadSucceededObserver {
            NotificationCenter.default.removeObserver(uploadSucceededObserver)
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
            .sorted(by: RecordingSummary.newestFirst)
    }

    /// Recordings grouped by calendar-day-start, sorted newest-day-first then
    /// newest-recording-first within each day. Recordings with no resolvable
    /// day (no `startedAt` and no parseable legacy `date` string) are dropped
    /// — they would otherwise bucket under `Date.distantPast` and a section
    /// header tap would navigate the calendar to ~4001 BC.
    func groupedByDay() -> [(day: Date, recordings: [RecordingSummary])] {
        let groups = Dictionary(grouping: recordings.compactMap { rec -> (Date, RecordingSummary)? in
            guard let day = rec.startedDay else { return nil }
            return (day, rec)
        }) { $0.0 }
        return groups
            .map { (day: $0.key, recordings: $0.value.map { $0.1 }.sorted(by: RecordingSummary.newestFirst)) }
            .sorted { $0.day > $1.day }
    }

    func refresh() async {
        guard !isLoading else { return }
        isLoading = true
        defer { isLoading = false }
        do {
            let rows: [RecordingSummary]
            do {
                rows = try await DaemonClient.recordingList().recordings
            } catch DaemonClientError.socketUnavailable, DaemonClientError.connectionFailed {
                rows = try await CLIClient.runJSON(["list", "--json"])
            }
            recordings = rows
            lastError = nil
        } catch {
            // Clear the cache even on failure so a delete-everything sweep
            // doesn't leave stale rows on the calendar / list. The MainWindow
            // detail distinguishes "loading", "error", and "empty" so the
            // user sees a banner with a Retry button instead of a misleading
            // welcome state.
            recordings = []
            lastError = error.localizedDescription
        }
    }

    /// Lets the UI dismiss a stale error banner without triggering another
    /// refresh. Used by the "Dismiss" button in MainWindow's error state.
    func clearError() {
        lastError = nil
    }
}
