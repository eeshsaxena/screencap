import Foundation

// SCR-174 U3 — light, local, rule-based query parsing. Turns a natural-phrasing
// query into structured filters: an absolute time window, an app/site filter,
// and the remaining free-text terms. Fully local — no ML, no network. Time
// expressions resolve in the parser's calendar/timezone (default: the device's,
// matching the recorder's clock). `now` is injected so behavior is deterministic
// under test. Unrecognized app tokens fall through to free-text by design.

/// An absolute time range in unix milliseconds (matches the daemon's
/// `start_ms`/`end_ms` and the recorders' `timestamp_ms`).
struct TimeWindow: Equatable, Sendable {
    let startMs: Int
    let endMs: Int
}

struct ParsedQuery: Equatable, Sendable {
    var timeWindow: TimeWindow?
    var appFilter: String?
    var freeText: String

    /// A query that parsed to nothing actionable (empty/whitespace input).
    var isEmpty: Bool { timeWindow == nil && appFilter == nil && freeText.isEmpty }
}

struct QueryParser {
    var calendar: Calendar

    init(calendar: Calendar = .current) {
        self.calendar = calendar
    }

    /// Recognized app/site tokens. Deliberately conservative — ambiguous common
    /// words (docs, mail, word, teams, sheets) are excluded so a query like
    /// "find the docs about X" doesn't misfire into an app filter. Extensible;
    /// full breadth is intentionally deferred (an on-device intent model is the
    /// post-v1 destination).
    private static let knownApps: [String] = [
        "salesforce", "slack", "zendesk", "gmail", "outlook", "figma", "notion",
        "looker", "jira", "confluence", "hubspot", "stripe", "github", "linear",
        "xcode", "zoom", "tableau", "airtable", "quip", "intercom", "1password",
    ]

    private static let partsOfDay: [(name: String, startHour: Int, endHour: Int)] = [
        ("morning", 6, 12), ("afternoon", 12, 18), ("evening", 18, 24),
    ]

    private static let weekdays: [(name: String, weekday: Int)] = [
        ("sunday", 1), ("monday", 2), ("tuesday", 3), ("wednesday", 4),
        ("thursday", 5), ("friday", 6), ("saturday", 7),
    ]

    func parse(_ raw: String, now: Date) -> ParsedQuery {
        let trimmed = raw.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !trimmed.isEmpty else {
            return ParsedQuery(timeWindow: nil, appFilter: nil, freeText: "")
        }

        // Pad with spaces so " token " matches at string boundaries too.
        var working = " \(trimmed.lowercased()) "

        let (window, afterTime) = extractTimeWindow(from: working, now: now)
        working = afterTime
        let (app, afterApp) = extractApp(from: working)
        working = afterApp

        let freeText = working
            .split(separator: " ")
            .joined(separator: " ")

        return ParsedQuery(timeWindow: window, appFilter: app, freeText: freeText)
    }

    // MARK: - Time

    private func extractTimeWindow(from s: String, now: Date) -> (TimeWindow?, String) {
        var working = s
        let yesterday = calendar.date(byAdding: .day, value: -1, to: now)!

        func strip(_ phrase: String) {
            working = working.replacingOccurrences(of: " \(phrase) ", with: " ")
        }

        // "<day> <part>" and "<part>" — most specific first.
        for part in Self.partsOfDay {
            if working.contains(" yesterday \(part.name) ") {
                strip("yesterday \(part.name)")
                return (dayPartWindow(day: yesterday, part: part), working)
            }
        }
        for part in Self.partsOfDay {
            if working.contains(" this \(part.name) ") {
                strip("this \(part.name)")
                return (dayPartWindow(day: now, part: part), working)
            }
            if working.contains(" \(part.name) ") {
                strip(part.name)
                return (dayPartWindow(day: now, part: part), working)
            }
        }

        if working.contains(" last week ") {
            strip("last week")
            return (weekWindow(now: now, offsetWeeks: -1), working)
        }
        if working.contains(" this week ") {
            strip("this week")
            return (weekWindow(now: now, offsetWeeks: 0), working)
        }

        for day in Self.weekdays {
            let phrase: String
            if working.contains(" last \(day.name) ") {
                phrase = "last \(day.name)"
            } else if working.contains(" \(day.name) ") {
                phrase = day.name
            } else {
                continue
            }
            strip(phrase)
            return (mostRecentWeekdayWindow(target: day.weekday, now: now), working)
        }

        if working.contains(" yesterday ") {
            strip("yesterday")
            return (fullDayWindow(day: yesterday), working)
        }
        if working.contains(" today ") {
            strip("today")
            return (fullDayWindow(day: now), working)
        }

        return (nil, working)
    }

    private func fullDayWindow(day: Date) -> TimeWindow {
        let start = calendar.startOfDay(for: day)
        let end = calendar.date(byAdding: .day, value: 1, to: start)!
        return TimeWindow(startMs: ms(start), endMs: ms(end))
    }

    private func dayPartWindow(day: Date, part: (name: String, startHour: Int, endHour: Int)) -> TimeWindow {
        let start0 = calendar.startOfDay(for: day)
        let start = calendar.date(byAdding: .hour, value: part.startHour, to: start0)!
        let end = calendar.date(byAdding: .hour, value: part.endHour, to: start0)!
        return TimeWindow(startMs: ms(start), endMs: ms(end))
    }

    private func weekWindow(now: Date, offsetWeeks: Int) -> TimeWindow {
        let comps = calendar.dateComponents([.yearForWeekOfYear, .weekOfYear], from: now)
        let startOfThisWeek = calendar.date(from: comps)!
        let start = calendar.date(byAdding: .weekOfYear, value: offsetWeeks, to: startOfThisWeek)!
        let end = calendar.date(byAdding: .weekOfYear, value: 1, to: start)!
        return TimeWindow(startMs: ms(start), endMs: ms(end))
    }

    /// The most recent past occurrence of `target` weekday, excluding today
    /// (so "last Tuesday" on a Tuesday means a week ago).
    private func mostRecentWeekdayWindow(target: Int, now: Date) -> TimeWindow {
        let todayWeekday = calendar.component(.weekday, from: now)
        var diff = (todayWeekday - target + 7) % 7
        if diff == 0 { diff = 7 }
        let day = calendar.date(byAdding: .day, value: -diff, to: now)!
        return fullDayWindow(day: day)
    }

    private func ms(_ date: Date) -> Int {
        Int((date.timeIntervalSince1970 * 1000).rounded())
    }

    // MARK: - App

    private func extractApp(from s: String) -> (String?, String) {
        var working = s
        for app in Self.knownApps where working.contains(" \(app) ") {
            working = working.replacingOccurrences(of: " \(app) ", with: " ")
            return (app, working)
        }
        return (nil, working)
    }
}
