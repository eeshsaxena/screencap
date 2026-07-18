import Foundation

// U12 (R13, KTD-1, KTD-11) — pure presentation helpers that turn a
// `(recording, timestamp_ms)` pointer into the day + time vocabulary the
// browsing + citation surfaces speak. The pointer itself is NEVER rewritten
// (KTD-1): the recording directory name stays the stable storage key; these
// helpers only DERIVE what a citation SHOWS and WHERE a tap lands (the day page,
// seeked). Factored out of the Chat / Recall-palette / Review views so the
// mapping is directly unit-testable (CitationTargetTests) and can never drift
// between Chat source cards, palette hit titles, the day cards, and the Review
// window title. No surface renders a recording entity (R5).

enum CitationTarget {

    /// The day + time label for an ANCHORED citation — "Today · 14:32",
    /// "Yesterday · 14:32", "Wednesday, 1 July · 14:32". The day half reuses
    /// `DaysModel.label` (the single day vocabulary, KTD-11); the time half is
    /// the local-timezone HH:mm of the pointer instant. Never a recording name.
    static func dayTimeLabel(
        anchorMs: Int,
        now: Date = Date(),
        calendar: Calendar = .current
    ) -> String {
        let date = Date(timeIntervalSince1970: Double(anchorMs) / 1000)
        let day = calendar.startOfDay(for: date)
        let dayLabel = DaysModel.label(for: day, now: now, calendar: calendar)
        return "\(dayLabel) · \(clock(date, calendar: calendar))"
    }

    /// The day-only label for an UNANCHORED citation (R13 edge): the exact
    /// moment is unknown, but the DAY is still honest — so the citation KEEPS
    /// its day and says the time is unknown, never dropped. "Today · time
    /// unknown".
    static func dayOnlyLabel(
        day: Date,
        now: Date = Date(),
        calendar: Calendar = .current
    ) -> String {
        "\(DaysModel.label(for: day, now: now, calendar: calendar)) · time unknown"
    }

    /// The local calendar day an absolute-ms pointer falls on — KTD-11's single
    /// day-mapping rule, shared with `RecallPalette.timelineTarget` and the Days
    /// cards, so a citation lands on the same day page a human would open.
    static func day(forAnchorMs ms: Int, calendar: Calendar = .current) -> Date {
        calendar.startOfDay(for: Date(timeIntervalSince1970: Double(ms) / 1000))
    }

    /// Local-timezone HH:mm for the citation's clock half.
    static func clock(_ date: Date, calendar: Calendar = .current) -> String {
        let f = DateFormatter()
        f.locale = Locale(identifier: "en_US_POSIX")
        f.dateFormat = "HH:mm"
        f.timeZone = calendar.timeZone
        return f.string(from: date)
    }

    /// The palette hit title (R13): prefer the daemon's window title, then the
    /// app name, then the MOMENT (day + time), then — for an unanchored hit
    /// whose recording day is resolvable — the day ("time unknown"). It NEVER
    /// falls back to the recording name (the retired `item.recording` fallback,
    /// R5). Pure so RecallPaletteRow's title is directly assertable.
    static func hitTitle(
        title: String?,
        app: String?,
        anchorMs: Int?,
        recordingDay: Date?,
        now: Date = Date(),
        calendar: Calendar = .current
    ) -> String {
        if let title, !title.isEmpty { return title }
        if let app, !app.isEmpty { return app }
        if let anchorMs { return dayTimeLabel(anchorMs: anchorMs, now: now, calendar: calendar) }
        if let recordingDay { return dayOnlyLabel(day: recordingDay, now: now, calendar: calendar) }
        // Truly unresolvable (no title/app, no anchor, no known day) — an honest
        // "time unknown", still never a recording identifier.
        return "Time unknown"
    }
}

/// R14 / AE4 — the Review & upload window titles itself with a day + time
/// label, NEVER the raw `rec-<timestamp>` recording name (recordings are retired
/// from the UI; the window is reached from a day badge / day-page footage, so it
/// must speak day + time at every entry point). Pure so the derivation is
/// directly unit-testable (CitationTargetTests) without a live window.
enum ReviewTitle {

    /// The window title for a recording. Prefers the recording's `startedAt`
    /// (absolute unix seconds); falls back to parsing the `rec-%Y%m%dT%H%M%S`
    /// directory name; and only when NEITHER resolves shows a bare "Review" — a
    /// recording identifier never surfaces in the title.
    static func title(
        startedAt: Double?,
        recordingName: String,
        now: Date = Date(),
        calendar: Calendar = .current
    ) -> String {
        guard let date = resolvedDate(
            startedAt: startedAt, recordingName: recordingName, calendar: calendar
        ) else {
            return "Review"
        }
        let day = calendar.startOfDay(for: date)
        let dayLabel = DaysModel.label(for: day, now: now, calendar: calendar)
        return "Review · \(dayLabel) · \(CitationTarget.clock(date, calendar: calendar))"
    }

    /// The instant this recording started, from `startedAt` when present and
    /// positive, else parsed from the `rec-%Y%m%dT%H%M%S` directory name. A
    /// zero/negative `startedAt` (null-timing sentinel) is treated as unknown.
    static func resolvedDate(
        startedAt: Double?,
        recordingName: String,
        calendar: Calendar = .current
    ) -> Date? {
        if let startedAt, startedAt > 0 {
            return Date(timeIntervalSince1970: startedAt)
        }
        return parseRecordingName(recordingName, calendar: calendar)
    }

    /// Best-effort parse of the CLI's `rec-%Y%m%dT%H%M%S` directory name into a
    /// local-time instant. Returns nil for any other shape (legacy / custom
    /// names) — the caller then shows a bare "Review", never the name itself.
    static func parseRecordingName(_ name: String, calendar: Calendar = .current) -> Date? {
        guard name.hasPrefix("rec-") else { return nil }
        let stamp = String(name.dropFirst("rec-".count))
        let f = DateFormatter()
        f.locale = Locale(identifier: "en_US_POSIX")
        f.dateFormat = "yyyyMMdd'T'HHmmss"
        f.timeZone = calendar.timeZone
        return f.date(from: stamp)
    }
}
