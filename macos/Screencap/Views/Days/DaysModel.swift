import Foundation

// U3 — the Days surface's pure model layer: day-clamped coverage grouping
// (KTD-6), day labels, the always-present Today card's live capture status
// (R15/AE6), the day-level upload/review badge (R14), and the task-aware
// title/summary helpers reused for the day cards' task summaries. Factored out
// of the view so the grouping / status / badge rules are directly unit-testable
// (DaysModelTests) without a render.
//
// Rescoped from the retired Journal model: day cards derive from day-clamped
// coverage, not start-day bucketing — a day whose only footage is an overnight
// tail still gets a card (KTD-6). No browsing surface shows a recording entity
// (R5): the day card carries the day's task summary, never a recording title.

enum DaysModel {

    // MARK: - Day cards (day-clamped coverage, KTD-6)

    /// One day's card: the day it represents, its heading label, whether it is
    /// today, and the recordings whose footage covers it (for task-summary and
    /// upload/review derivation — never rendered as recording entities, R5).
    struct DayCard: Identifiable, Equatable {
        /// Local-calendar start of day.
        let day: Date
        let label: String
        let isToday: Bool
        let recordings: [RecordingSummary]

        var id: String {
            let f = ISO8601DateFormatter()
            f.formatOptions = [.withFullDate]
            return f.string(from: day)
        }
    }

    /// The local calendar days a recording's footage covers, clamped to whole
    /// days (KTD-6). A 23:50 → 00:10 recording covers BOTH its start day and the
    /// next — so an overnight tail still yields a card on the following day.
    /// Undated recordings (no derivable start) cover nothing here; they are
    /// reachable through the Inspect debug entry (KTD-10), never bucketed into a
    /// fabricated date.
    static func coveredDays(for rec: RecordingSummary, calendar: Calendar = .current) -> [Date] {
        guard let baseDay = rec.startedDay else { return [] }
        // A legacy `date`-only recording (no `started_at`) can't have its span
        // computed — it covers only its single derived day.
        guard let startTs = rec.startedAt else { return [baseDay] }
        let start = Date(timeIntervalSince1970: startTs)
        let end = start.addingTimeInterval(max(0, rec.durationSeconds ?? 0))
        let startDay = calendar.startOfDay(for: start)
        let endDay = calendar.startOfDay(for: end)
        var days: [Date] = []
        var cursor = startDay
        // Guard against an unbounded loop from a pathological span.
        var safety = 0
        while cursor <= endDay, safety < 400 {
            days.append(cursor)
            guard let next = calendar.date(byAdding: .day, value: 1, to: cursor) else { break }
            cursor = next
            safety += 1
        }
        return days.isEmpty ? [baseDay] : days
    }

    /// Group recordings into day cards, newest day first, by day-clamped coverage
    /// (KTD-6). A recording appears on every day its footage spans. Within a day,
    /// recordings order chronologically (morning → evening) to match the day
    /// timeline's axis. Undated recordings are excluded (reachable via Inspect).
    static func dayCards(
        _ recordings: [RecordingSummary],
        now: Date = Date(),
        calendar: Calendar = .current
    ) -> [DayCard] {
        var buckets: [Date: [RecordingSummary]] = [:]
        for rec in recordings {
            for day in coveredDays(for: rec, calendar: calendar) {
                buckets[day, default: []].append(rec)
            }
        }
        let today = calendar.startOfDay(for: now)
        return buckets
            .sorted { $0.key > $1.key }
            .map { day, recs in
                DayCard(
                    day: day,
                    label: label(for: day, now: now, calendar: calendar),
                    isToday: day == today,
                    recordings: recs.sorted { ($0.startedAt ?? 0) < ($1.startedAt ?? 0) }
                )
            }
    }

    /// Today / Yesterday / "Wednesday, 1 July" (+ year when not this year) — the
    /// Newsreader day-heading vocabulary.
    static func label(for day: Date, now: Date, calendar: Calendar = .current) -> String {
        let today = calendar.startOfDay(for: now)
        if day == today { return "Today" }
        if let yesterday = calendar.date(byAdding: .day, value: -1, to: today),
           day == yesterday { return "Yesterday" }
        let sameYear = calendar.component(.year, from: day) == calendar.component(.year, from: now)
        let formatter = DateFormatter()
        formatter.locale = Locale(identifier: "en_US")
        formatter.calendar = calendar
        formatter.timeZone = calendar.timeZone
        formatter.dateFormat = sameYear ? "EEEE, d MMMM" : "EEEE, d MMMM yyyy"
        return formatter.string(from: day)
    }

    // MARK: - Today card live capture status (R15 / AE6)

    /// The Today card's live capture status. `paused` is DISTINCT from `off`
    /// (AE6): an ambient recording paused mid-session reads as "Paused", never as
    /// "ambient recording is off". `starting` covers ambient enabled-but-not-yet
    /// -active so an honest transient never mislabels itself "off".
    enum TodayCaptureStatus: Equatable {
        /// A recording is live (ambient or manual). `since` is its start instant,
        /// nil when not yet known (index not refreshed) → "Recording now".
        case recording(since: Date?)
        /// Ambient capture is live but paused — nothing is being captured.
        case paused
        /// Ambient is enabled but no live recording exists yet (spawning).
        case starting
        /// Ambient recording is off and nothing is recording.
        case off
    }

    /// Resolve the Today-card status from the recorder + ambient state. Pure so
    /// the AE6 states are assertable without a live daemon. Paused takes
    /// precedence — `AmbientStatus.paused` is only ever true while an ambient
    /// recording is live, so it always means "paused", not "off".
    static func todayCaptureStatus(
        isRecording: Bool,
        startedAt: Date?,
        ambientEnabled: Bool,
        ambientActive: Bool,
        ambientPaused: Bool
    ) -> TodayCaptureStatus {
        if ambientPaused { return .paused }
        if isRecording || ambientActive { return .recording(since: startedAt) }
        if ambientEnabled { return .starting }
        return .off
    }

    /// The Today-card status line copy for each state (R15 vocabulary).
    static func todayStatusLine(_ status: TodayCaptureStatus) -> String {
        switch status {
        case .recording(let since):
            guard let since else { return "Recording now" }
            let f = DateFormatter()
            f.dateFormat = "HH:mm"
            f.timeZone = .current
            return "Recording since \(f.string(from: since))"
        case .paused:
            return "Paused — nothing is being captured until you resume"
        case .starting:
            return "Ambient recording is starting…"
        case .off:
            return "Ambient recording is off"
        }
    }

    // MARK: - Day-level upload / review badge (R14)

    /// The day-level upload/review status (R14). The UPLOAD arm is scoped to
    /// cloud-destined footage per the frozen policy — a local-only-policy day
    /// carries NO permanent pending-upload badge. `reviewTarget` is the recording
    /// the Review & upload window opens on (the newest cloud-pending recording).
    struct DayUploadReview: Equatable {
        /// Cloud-destined recordings on the day not yet uploaded (upload-eligible).
        let cloudPendingCount: Int
        /// Recording name to open in Review & upload, or nil when nothing pends.
        let reviewTarget: String?

        var isActionable: Bool { cloudPendingCount > 0 && reviewTarget != nil }

        /// The mono badge line, e.g. "2 not yet uploaded · needs review".
        var badgeText: String? {
            guard cloudPendingCount > 0 else { return nil }
            let noun = cloudPendingCount == 1 ? "recording" : "recordings"
            return "\(cloudPendingCount) \(noun) not yet uploaded · needs review"
        }
    }

    /// Whether a recording's frozen policy sends it to the cloud (R14): the
    /// intent destination is `cloud` or `both`. A `local` or absent intent is
    /// local-only — never counted toward the upload arm.
    static func isCloudDestined(_ rec: RecordingSummary) -> Bool {
        rec.intent == "cloud" || rec.intent == "both"
    }

    /// Aggregate a day's recordings into the upload/review badge (R14). Only
    /// cloud-destined, upload-eligible (not uploaded, not a stub) recordings
    /// count; the review target is the newest such recording.
    static func uploadReview(_ recordings: [RecordingSummary]) -> DayUploadReview {
        let pending = recordings
            .filter { isCloudDestined($0) && $0.isUploadEligible }
            .sorted(by: RecordingSummary.newestFirst)
        return DayUploadReview(
            cloudPendingCount: pending.count,
            reviewTarget: pending.first?.name
        )
    }

    // MARK: - Task-aware title / summary helpers (reused for day task summaries)

    /// The card's summary line — the recording's task description, hidden entirely
    /// when the recording has none.
    static func summaryLine(_ rec: RecordingSummary) -> String? {
        guard let summary = rec.summary?.trimmingCharacters(in: .whitespacesAndNewlines),
              !summary.isEmpty else { return nil }
        return summary
    }

    /// The display title for a recording, preferring its user-set title and
    /// falling back to a locally-named task when the recording is otherwise
    /// un-named. Retained (used by the task-summary derivation and pinned by
    /// tests); the day cards themselves never render a recording title (R5).
    static func displayTitle(_ rec: RecordingSummary, tasks: [RecordingTask]) -> String {
        let hasUserTitle = rec.titleIsUserSet ?? (rec.title != rec.name)
        if hasUserTitle { return rec.title }
        if let first = tasks.first?.name.trimmingCharacters(in: .whitespacesAndNewlines),
           !first.isEmpty {
            return first
        }
        return rec.title
    }

    /// The summary line preferring the recording's `summary` and falling back to a
    /// compact "N tasks" line from the locally-named tasks. Nil (line hidden) only
    /// when there is neither a summary nor any task.
    static func summaryLine(_ rec: RecordingSummary, tasks: [RecordingTask]) -> String? {
        if let summary = summaryLine(rec) { return summary }
        return taskSummaryLine(tasks.map(\.name))
    }

    /// A compact one-line summary of task names: "First" for one, "First · +N
    /// more" for several. Nil when there are no non-empty names. Used by the day
    /// card to summarize the day's tasks without naming any recording.
    static func taskSummaryLine(_ names: [String]) -> String? {
        let named = names
            .map { $0.trimmingCharacters(in: .whitespacesAndNewlines) }
            .filter { !$0.isEmpty }
        guard !named.isEmpty else { return nil }
        if named.count == 1 { return named[0] }
        return "\(named[0]) · +\(named.count - 1) more"
    }

    /// The dominant app across a recording's `timeline.query` rows — the day
    /// card's app chip. Most frequent non-empty `app`, first-seen winning ties;
    /// nil when no row names an app.
    static func dominantApp(_ rows: [TimelineRow]) -> String? {
        var counts: [String: (count: Int, firstIndex: Int)] = [:]
        for (i, row) in rows.enumerated() {
            guard let app = row.app, !app.isEmpty else { continue }
            if let existing = counts[app] {
                counts[app] = (existing.count + 1, existing.firstIndex)
            } else {
                counts[app] = (1, i)
            }
        }
        return counts
            .max { a, b in
                if a.value.count != b.value.count { return a.value.count < b.value.count }
                return a.value.firstIndex > b.value.firstIndex
            }?
            .key
    }
}
