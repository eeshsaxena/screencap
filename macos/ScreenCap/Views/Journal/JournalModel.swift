import Foundation

// U8 — the Journal's pure model layer: day grouping, day labels, the mono count
// string, and the dominant-app derivation for the card's app chip. Factored out
// of `JournalView` so the grouping rules are directly unit-testable
// (JournalGroupingTests) without a render.

enum JournalModel {

    /// One day section (design 395–417, logic 743–753): a heading label, the
    /// mono "N recordings" count, and the day's cards.
    struct Day: Identifiable, Equatable {
        /// Local-calendar start of day, nil for rows whose start time can't be
        /// derived at all (no `started_at` and an unparseable legacy `date`).
        let day: Date?
        let label: String
        let items: [RecordingSummary]

        var id: String { label }

        /// The mono count string next to the day heading (design 397).
        var countText: String {
            items.count == 1 ? "1 recording" : "\(items.count) recordings"
        }
    }

    /// Group recordings into day sections, newest day first. A recording belongs
    /// to the local calendar day it *started* on (a 23:50 recording that runs
    /// past midnight stays under its start day). Within a day, cards read
    /// left-to-right chronologically — matching the day timeline's
    /// morning-to-evening axis rather than the Library's newest-first grid.
    static func days(
        _ recordings: [RecordingSummary],
        now: Date = Date(),
        calendar: Calendar = .current
    ) -> [Day] {
        var buckets: [Date: [RecordingSummary]] = [:]
        var undated: [RecordingSummary] = []
        for rec in recordings {
            if let day = rec.startedDay {
                buckets[day, default: []].append(rec)
            } else {
                undated.append(rec)
            }
        }
        var days: [Day] = buckets
            .sorted { $0.key > $1.key }
            .map { day, items in
                Day(
                    day: day,
                    label: label(for: day, now: now, calendar: calendar),
                    items: items.sorted { ($0.startedAt ?? 0) < ($1.startedAt ?? 0) }
                )
            }
        if !undated.isEmpty {
            // Never hide a recording just because its start time is unknowable —
            // park it in a trailing section rather than fabricating a date.
            days.append(Day(day: nil, label: "Undated", items: undated))
        }
        return days
    }

    /// Today / Yesterday / "Wednesday, 1 July" (+ year when not this year) — the
    /// Newsreader day-heading vocabulary (design 396, Day-timeline header 428).
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

    /// The card's summary line — U2's `summary` (the namer's task description),
    /// hidden entirely when the namer hasn't produced one (design 409 renders it
    /// only in the prototype's always-populated mock).
    static func summaryLine(_ rec: RecordingSummary) -> String? {
        guard let summary = rec.summary?.trimmingCharacters(in: .whitespacesAndNewlines),
              !summary.isEmpty else { return nil }
        return summary
    }

    /// The dominant app across a recording's `timeline.query` rows — the app
    /// chip (design 412). Most frequent non-empty `app`, first-seen winning
    /// ties; nil (chip omitted) when no row names an app.
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
