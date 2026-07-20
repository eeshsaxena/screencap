import Foundation

// U6 — the Tasks surface's pure model layer: the reverse-chronological
// day-grouping of cross-day task segments, the LOCAL substring filter (with the
// filter-zero-vs-system-zero distinction, R21), and the honest zero/degraded
// state mapping from the per-recording rollup (RecordingHonestState vocabulary).
// Factored out of the view so the grouping / filter / honest-state rules are
// directly unit-testable (TasksModelTests) without a render or a live daemon.
//
// No browsing surface shows a recording entity (R5): a TaskRow carries its
// recording key ONLY as opaque plumbing for seek + curation, never for display —
// the row renders the task name, its day, and its wall-clock range.
enum TasksModel {

    // MARK: - Row + day-group view models

    /// One task row. `recording` / `recordingId` / `taskIndex` are the pointer +
    /// curation key (KTD-1) — never rendered (R5). `startTs` / `endTs` are Unix
    /// seconds; `day` is the LOCAL start-of-day the task maps to (KTD-11), used for
    /// the click → day-page route.
    struct TaskRow: Identifiable, Equatable {
        let recording: String
        let recordingId: String?
        let taskIndex: Int
        let name: String
        let category: String?
        let startTs: Double
        let endTs: Double
        let day: Date

        var id: String { "\(recording)#\(taskIndex)" }

        /// Task span in absolute unix ms — the seek anchor + the highlight extent
        /// the day page emphasizes on landing (AE3).
        var startMs: Int { Int((startTs * 1000).rounded()) }
        var endMs: Int { Int((endTs * 1000).rounded()) }

        /// The landing highlight span for the day page (AE3).
        var highlight: DaySpanHighlight { DaySpanHighlight(startMs: startMs, endMs: endMs) }

        /// "HH:mm–HH:mm" over the task's local wall-clock window (never a recording
        /// name, R5). A sub-minute span collapses to a single time.
        var timeRangeText: String {
            let start = TasksModel.clock(startTs)
            let end = TasksModel.clock(endTs)
            return start == end ? start : "\(start)–\(end)"
        }
    }

    /// One day's group of task rows: the raw date key, its heading label
    /// (Today / Yesterday / "Wednesday, 1 July"), and the rows (start-ordered).
    struct DayGroup: Identifiable, Equatable {
        let date: String
        let label: String
        let rows: [TaskRow]

        var id: String { date }
    }

    // MARK: - Grouping (reverse-chronological, KTD-11 day rule preserved)

    /// Map the daemon's per-day task payload into presentation day-groups. The
    /// daemon already groups by the local calendar day (KTD-11) and returns days
    /// newest-first with tasks start-ordered; this preserves that order and only
    /// derives the human day label + the per-row `day` Date. A day whose date fails
    /// to parse is skipped (never fabricated).
    static func dayGroups(
        from days: [TasksQueryDay],
        now: Date = Date(),
        calendar: Calendar = .current
    ) -> [DayGroup] {
        days.compactMap { day in
            guard let dayDate = parseDay(day.date, calendar: calendar) else { return nil }
            let rows = day.tasks.map { task in
                TaskRow(
                    recording: task.recording,
                    recordingId: task.recordingId,
                    taskIndex: task.taskIndex,
                    name: task.name,
                    category: task.category,
                    startTs: task.startTs,
                    endTs: task.endTs,
                    day: dayDate
                )
            }
            return DayGroup(
                date: day.date,
                label: DaysModel.label(for: dayDate, now: now, calendar: calendar),
                rows: rows
            )
        }
    }

    // MARK: - Local substring filter (browse-verb data only — no 402 recall verbs)

    /// Narrow loaded day-groups to rows whose name OR category contains `query`
    /// (case/diacritic-insensitive), dropping days left with no match. An empty /
    /// whitespace query returns the groups unchanged. Operates ENTIRELY over the
    /// already-loaded browse data — free-tier Tasks search must work, so no
    /// paid recall verb is ever hit here.
    static func filter(_ groups: [DayGroup], query: String) -> [DayGroup] {
        let needle = query.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !needle.isEmpty else { return groups }
        return groups.compactMap { group in
            let matched = group.rows.filter { matches($0, needle: needle) }
            return matched.isEmpty ? nil : DayGroup(date: group.date, label: group.label, rows: matched)
        }
    }

    /// Substring match over the row's task name and category.
    private static func matches(_ row: TaskRow, needle: String) -> Bool {
        let options: String.CompareOptions = [.caseInsensitive, .diacriticInsensitive]
        if row.name.range(of: needle, options: options) != nil { return true }
        if let category = row.category, category.range(of: needle, options: options) != nil { return true }
        return false
    }

    // MARK: - Resolved list state (R21: filter-zero vs system-zero)

    /// The Tasks list's resolved presentation state. The filter-zero and
    /// system-zero cases are DISTINCT (R21): a filter that matches nothing among
    /// existing tasks must never render the system-wide zero copy (which would
    /// misread as data loss).
    enum ListState: Equatable {
        /// Day-groups to render (already filtered when a query is active).
        case populated([DayGroup])
        /// Tasks exist in the window, but the active filter matched none of them.
        case filterZero(query: String)
        /// No tasks are loaded at all — the honest system-wide zero/degraded state.
        case systemZero(SystemZero)
    }

    /// Resolve what the list should show from the loaded response + the active
    /// filter. Pure so the filter-zero-vs-system-zero rule is directly assertable.
    static func listState(
        days: [TasksQueryDay],
        recordings: [TasksQueryRecordingStatus],
        query: String,
        verdict: IntelligenceVerdict?,
        now: Date = Date(),
        calendar: Calendar = .current
    ) -> ListState {
        let groups = dayGroups(from: days, now: now, calendar: calendar)
        let hasAnyTask = groups.contains { !$0.rows.isEmpty }
        let needle = query.trimmingCharacters(in: .whitespacesAndNewlines)

        // No tasks loaded at all → the honest system-wide zero state, regardless of
        // any filter text (an empty library filtered is still an empty library).
        guard hasAnyTask else {
            return .systemZero(systemZero(recordings: recordings, verdict: verdict))
        }
        guard !needle.isEmpty else { return .populated(groups) }
        let filtered = filter(groups, query: needle)
        // Tasks EXIST but none match the filter → filter-zero (never system-zero).
        return filtered.isEmpty ? .filterZero(query: needle) : .populated(filtered)
    }

    // MARK: - Honest system-wide zero / degraded state (R21)

    /// The honest copy for a system-wide zero-task state — derived from the
    /// per-recording rollup + the live intelligence verdict, NEVER "you did
    /// nothing".
    struct SystemZero: Equatable {
        let state: RecordingHonestState
        let title: String
        let message: String
        /// Whether to offer a "Set up intelligence" affordance (only `.notSetUp`).
        let offersSetup: Bool
        /// Whether the state is a transient/retryable one (in-progress / failed run)
        /// — the surface can offer a Refresh rather than a set-up action.
        let isRetryable: Bool
    }

    /// Compose the system-wide zero state. An empty rollup (no recording covers the
    /// range) is "nothing on file" — distinct from a rollup that produced no names.
    static func systemZero(
        recordings: [TasksQueryRecordingStatus],
        verdict: IntelligenceVerdict?
    ) -> SystemZero {
        guard !recordings.isEmpty else {
            return SystemZero(
                state: .unknown,
                title: "Nothing on file for this range",
                message: "No recordings cover these days yet. Record something, or widen the "
                    + "range to see older tasks.",
                offersSetup: false,
                isRetryable: false
            )
        }
        let state = aggregateHonestState(recordings: recordings, verdict: verdict)
        switch state {
        case .notSetUp:
            return SystemZero(
                state: state,
                title: "Set up intelligence to name your tasks",
                message: "Your days are recorded and searchable. Turn on on-device or cloud "
                    + "intelligence and Screencap will name the tasks in them.",
                offersSetup: true,
                isRetryable: false
            )
        case .couldntRun:
            return SystemZero(
                state: state,
                title: "Task naming didn't finish",
                message: "The last attempt to name your tasks didn't complete. Screencap retries "
                    + "automatically — check back shortly, or refresh to try again.",
                offersSetup: false,
                isRetryable: true
            )
        case .inProgress:
            return SystemZero(
                state: state,
                title: "Still naming your tasks…",
                message: "Screencap is working through your recent days. Named tasks appear here "
                    + "as they're ready.",
                offersSetup: false,
                isRetryable: true
            )
        case .nothingToName:
            return SystemZero(
                state: state,
                title: "No tasks to name here yet",
                message: "Screencap looked through this range and didn't find distinct tasks to "
                    + "name. Your footage is still recorded and searchable.",
                offersSetup: false,
                isRetryable: false
            )
        default:
            // producedTasks / partial / mechanical (tasks that fell outside the
            // window's start filter) and unknown all read as the neutral
            // no-tasks-here state — never a false "not set up" or "you did nothing".
            return SystemZero(
                state: .unknown,
                title: "No named tasks in this range",
                message: "Tasks you record and name appear here, newest first. Try a wider range "
                    + "to see older tasks.",
                offersSetup: false,
                isRetryable: false
            )
        }
    }

    /// Aggregate the rollup into a single honest state, most-actionable-first, so a
    /// range with one un-set-up recording reads "set up intelligence" rather than
    /// the quietest state on the day. Resolves each entry through the shared
    /// `RecordingHonestState` rule (so the Tasks surface and the day page can't
    /// drift) then picks by priority.
    static func aggregateHonestState(
        recordings: [TasksQueryRecordingStatus],
        verdict: IntelligenceVerdict?
    ) -> RecordingHonestState {
        let states = recordings.map {
            RecordingHonestState.resolve(reason: $0.reason, detail: $0.detail, verdict: verdict)
        }
        return states.max { priority($0) < priority($1) } ?? .unknown
    }

    /// Actionability ranking for the zero-state aggregation. Set-up wins (the key
    /// onboarding action), then a failed run, then an in-flight pass, then the
    /// benign "nothing to name"; task-bearing / unknown states rank lowest since
    /// they carry no zero-state guidance.
    private static func priority(_ state: RecordingHonestState) -> Int {
        switch state {
        case .notSetUp: return 5
        case .couldntRun: return 4
        case .inProgress: return 3
        case .nothingToName: return 2
        case .producedTasks, .producedTasksPartial, .mechanicalOnly, .unknown: return 1
        }
    }

    // MARK: - Helpers

    /// "HH:mm" for a Unix-seconds instant in the local timezone.
    static func clock(_ ts: Double) -> String {
        let f = DateFormatter()
        f.locale = Locale(identifier: "en_US")
        f.dateFormat = "HH:mm"
        f.timeZone = .current
        return f.string(from: Date(timeIntervalSince1970: ts))
    }

    /// Parse a `YYYY-MM-DD` local calendar date into its LOCAL start-of-day Date
    /// (KTD-11 — the same day rule the daemon grouped by). `nil` for a malformed
    /// date, so a decode surprise skips the day rather than fabricating one.
    static func parseDay(_ date: String, calendar: Calendar = .current) -> Date? {
        let f = DateFormatter()
        f.locale = Locale(identifier: "en_US_POSIX")
        f.calendar = calendar
        f.timeZone = calendar.timeZone
        f.dateFormat = "yyyy-MM-dd"
        guard let parsed = f.date(from: date) else { return nil }
        return calendar.startOfDay(for: parsed)
    }

    /// Format a local `Date` as the `YYYY-MM-DD` the query verb expects.
    static func dateKey(_ day: Date, calendar: Calendar = .current) -> String {
        let f = DateFormatter()
        f.locale = Locale(identifier: "en_US_POSIX")
        f.calendar = calendar
        f.timeZone = calendar.timeZone
        f.dateFormat = "yyyy-MM-dd"
        return f.string(from: day)
    }
}
