import Foundation

// Moments — the merged surface's pure model layer. Unions the app-detected task
// segments (`tasks.query`) with the ranges the user clipped (`clip.list`) into one
// reverse-chronological, day-grouped list interleaved by FOOTAGE time (KTD-3), and
// resolves the honest zero/filter state (KTD-6). Factored out of the view so the
// merge / sort / group / filter / zero-state rules are directly unit-testable
// (MomentsModelTests) without a render or a live daemon.
//
// It reuses `TasksModel` (task-row construction, the day rule, the honest
// system-zero vocabulary) and `ClipsModel` (source-day label + range formatting)
// so the two halves can't drift from their origin surfaces. No recording entity is
// ever surfaced (R5): a row shows its name/range + day, never a recording.
enum MomentsModel {

    // MARK: - Row + day-group view models

    /// One row in the merged list — either an app-detected task span or a range the
    /// user clipped. The associated value keeps the full source record so the view
    /// can render + act on each kind (auto: open day + rename/delete; clipped: play
    /// / export / share / delete), while the computed properties below normalize the
    /// two into a common shape for sorting, grouping, and display.
    enum MomentRow: Identifiable, Equatable {
        case auto(TasksModel.TaskRow)
        case clipped(ClipRecord)

        /// Namespaced so an app-detected `recording#index` and a clip uuid can never
        /// collide in one list.
        var id: String {
            switch self {
            case .auto(let row): return "auto:\(row.id)"
            case .clipped(let clip): return "clip:\(clip.id)"
            }
        }

        /// The marker the row carries (R3): only user-clipped rows are marked.
        var isClipped: Bool {
            if case .clipped = self { return true }
            return false
        }

        /// Footage start in absolute unix ms — the common interleave key (KTD-3).
        /// An app-detected row's Unix-seconds `start_ts` is scaled to ms so a task
        /// and a clip on the same instant land adjacent.
        var startMs: Int {
            switch self {
            case .auto(let row): return row.startMs
            case .clipped(let clip): return clip.startMs
            }
        }

        var endMs: Int {
            switch self {
            case .auto(let row): return row.endMs
            case .clipped(let clip): return clip.endMs
            }
        }

        /// "HH:mm–HH:mm" over the row's footage window (never a recording name, R5),
        /// formatted by each kind's origin model so the shape can't drift.
        var timeRangeText: String {
            switch self {
            case .auto(let row): return row.timeRangeText
            case .clipped(let clip): return ClipsModel.rangeClockText(clip)
            }
        }

        /// The LOCAL day the row maps to, for the click → day-page route (R4). An
        /// app-detected row always resolves; a clipped row resolves via its parsed
        /// `source_day` (nil only for a malformed key, in which case the row is not
        /// day-navigable but still plays/exports in place).
        var day: Date? {
            switch self {
            case .auto(let row): return row.day
            case .clipped(let clip): return ClipsModel.parseSourceDay(clip.sourceDay)
            }
        }

        /// The day-page landing highlight span (AE3) for either row kind.
        var highlight: DaySpanHighlight { DaySpanHighlight(startMs: startMs, endMs: endMs) }
    }

    /// One day's group of merged rows: the raw `yyyy-MM-dd` key, its heading label
    /// (Today / Yesterday / "Wednesday, 1 July"), and the rows (footage-time
    /// ascending within the day).
    struct MomentDayGroup: Identifiable, Equatable {
        let dayKey: String
        let label: String
        let rows: [MomentRow]

        var id: String { dayKey }
    }

    // MARK: - Merge (union → footage-time interleave → day groups)

    /// Union the app-detected day payload and the clip catalog into one list of
    /// day-groups. Both halves key on the same `yyyy-MM-dd` local-day rule (KTD-11 —
    /// task days from `TasksQueryDay.date`, clip days from `ClipRecord.source_day`),
    /// so a task and a clip on the same calendar day share a group. Days are
    /// newest-first; within a day, rows are FOOTAGE-time ascending (KTD-3) with a
    /// stable id tie-break, so a clip sits where the moment happened among that day's
    /// tasks — not where/when it was cut.
    ///
    /// Because `clip.list` returns every clip while `tasks.query` is windowed
    /// (KTD-2), scrolling into the deep past yields days that hold only Clipped rows
    /// (R6) — the durable moments outlive their day's evicted task segments.
    static func merge(
        days: [TasksQueryDay],
        clips: [ClipRecord],
        now: Date = Date(),
        calendar: Calendar = .current
    ) -> [MomentDayGroup] {
        var buckets: [String: [MomentRow]] = [:]
        var labels: [String: String] = [:]

        // App-detected rows, reusing TasksModel's construction + day rule + label.
        for group in TasksModel.dayGroups(from: days, now: now, calendar: calendar) {
            buckets[group.date, default: []].append(contentsOf: group.rows.map(MomentRow.auto))
            labels[group.date] = group.label
        }

        // Clipped rows, keyed on the same day rule; ClipsModel's label matches
        // TasksModel's for a day that also has tasks (both go through DaysModel.label).
        for clip in clips {
            buckets[clip.sourceDay, default: []].append(.clipped(clip))
            if labels[clip.sourceDay] == nil {
                labels[clip.sourceDay] = ClipsModel.dayLabel(clip.sourceDay, now: now, calendar: calendar)
            }
        }

        return buckets.keys.sorted(by: >).map { key in
            let rows = buckets[key]!.sorted { lhs, rhs in
                lhs.startMs != rhs.startMs ? lhs.startMs < rhs.startMs : lhs.id < rhs.id
            }
            return MomentDayGroup(dayKey: key, label: labels[key] ?? key, rows: rows)
        }
    }

    // MARK: - Filter (type + substring)

    /// Narrow the merged groups by the active filter, dropping days left with no
    /// match. `clippedOnly` keeps only user-clipped rows (R5). A non-empty `query`
    /// substring-matches app-detected rows by name/category (the carried-over Tasks
    /// filter); a clipped row carries no searchable name, so a substring query
    /// excludes it — the Clipped type-filter, not a text search, is the tool for
    /// focusing on kept moments. With neither active, groups pass through unchanged.
    static func filter(_ groups: [MomentDayGroup], query: String, clippedOnly: Bool) -> [MomentDayGroup] {
        // The Clipped filter and the substring filter are mutually exclusive in the
        // UI (the substring field is hidden under Clipped), so ignore any stale query
        // when clippedOnly — otherwise a leftover name search would drop every clip
        // (clips carry no matchable name) and falsely read as "no clipped moments" (R5).
        let needle = clippedOnly ? "" : query.trimmingCharacters(in: .whitespacesAndNewlines)
        guard clippedOnly || !needle.isEmpty else { return groups }
        return groups.compactMap { group in
            let rows = group.rows.filter { row in
                if clippedOnly && !row.isClipped { return false }
                if !needle.isEmpty && !matches(row, needle: needle) { return false }
                return true
            }
            return rows.isEmpty ? nil : MomentDayGroup(dayKey: group.dayKey, label: group.label, rows: rows)
        }
    }

    /// Substring match over an app-detected row's task name/category. Clipped rows
    /// have no name to match, so they never satisfy a text query (see `filter`).
    private static func matches(_ row: MomentRow, needle: String) -> Bool {
        guard case .auto(let task) = row else { return false }
        let options: String.CompareOptions = [.caseInsensitive, .diacriticInsensitive]
        if task.name.range(of: needle, options: options) != nil { return true }
        if let category = task.category, category.range(of: needle, options: options) != nil { return true }
        return false
    }

    // MARK: - Resolved list state (R7/KTD-6, R21: filter-zero vs system-zero)

    /// The Moments list's resolved presentation state. Filter-zero and system-zero
    /// are DISTINCT (R21): a filter that matched nothing must never render the
    /// system-wide zero copy (which would misread as "you have nothing").
    enum ListState: Equatable {
        /// Day-groups to render (already filtered when a filter is active).
        case populated([MomentDayGroup])
        /// Rows exist, but the active filter matched none of them.
        case filterZero(query: String, clippedOnly: Bool)
        /// No rows at all — the honest system-wide zero, reusing the Tasks
        /// vocabulary (KTD-6): with no clips present, an Intelligence-off library
        /// shows the enable-Intelligence prompt (AE1).
        case systemZero(TasksModel.SystemZero)
    }

    /// Resolve what the list should show from the two loaded sources + the active
    /// filter. Pure, so the clips-override-Intelligence (KTD-6) and the
    /// filter-zero-vs-system-zero (R21) rules are directly assertable.
    ///
    /// Any clip produces a populated group, so the system-zero branch is reached
    /// only when there are genuinely no kept moments AND no named tasks — exactly
    /// where the enable-Intelligence prompt belongs (R7/AE1). Store-state (sealed /
    /// absent / error) is branched by the view BEFORE this call (KTD-6), so it is
    /// not an input here.
    static func listState(
        days: [TasksQueryDay],
        clips: [ClipRecord],
        recordings: [TasksQueryRecordingStatus],
        query: String = "",
        clippedOnly: Bool = false,
        verdict: IntelligenceVerdict? = nil,
        now: Date = Date(),
        calendar: Calendar = .current
    ) -> ListState {
        let groups = merge(days: days, clips: clips, now: now, calendar: calendar)
        let hasAnyRow = groups.contains { !$0.rows.isEmpty }

        guard hasAnyRow else {
            return .systemZero(TasksModel.systemZero(recordings: recordings, verdict: verdict))
        }

        // clippedOnly ignores any (hidden, stale) substring query — the two filters
        // are mutually exclusive in the UI, so a leftover query must never hide clips.
        let needle = clippedOnly ? "" : query.trimmingCharacters(in: .whitespacesAndNewlines)
        guard clippedOnly || !needle.isEmpty else { return .populated(groups) }

        let filtered = filter(groups, query: query, clippedOnly: clippedOnly)
        return filtered.isEmpty
            ? .filterZero(query: needle, clippedOnly: clippedOnly)
            : .populated(filtered)
    }
}
