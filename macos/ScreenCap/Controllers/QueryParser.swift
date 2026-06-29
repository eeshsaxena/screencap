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

    /// Month names + common abbreviations → month number. ("may" has no distinct
    /// abbreviation.) Used for explicit dates ("June 3") and date ranges.
    private static let months: [String: Int] = [
        "january": 1, "february": 2, "march": 3, "april": 4, "may": 5,
        "june": 6, "july": 7, "august": 8, "september": 9, "october": 10,
        "november": 11, "december": 12,
        "jan": 1, "feb": 2, "mar": 3, "apr": 4, "jun": 6, "jul": 7,
        "aug": 8, "sep": 9, "sept": 9, "oct": 10, "nov": 11, "dec": 12,
    ]

    /// Words separating the two endpoints of an explicit-date range.
    private static let rangeSeparators: Set<String> = ["to", "through", "until", "-"]

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

        // Token view of the (still-unmodified) query for the structured
        // date/relative branches below. Nothing above has stripped on a
        // non-returning path, so these tokens match `working`.
        let tokens = working.split(separator: " ").map(String.init)

        // Explicit-date RANGE ("june 1 to june 3") — before single dates, else
        // the single-date branch would consume one endpoint.
        if let r = findDateRange(tokens) {
            strip(r.phrase)
            return (dateRangeWindow(start: r.start, end: r.end, now: now), working)
        }

        // Single explicit date ("june 3" / "3 june").
        if let d = findExplicitDate(tokens) {
            strip(d.phrase)
            return (explicitDateWindow(month: d.month, day: d.day, now: now), working)
        }

        // "<n> days ago" — a single full day N days back.
        if let a = findDaysAgo(tokens) {
            strip(a.phrase)
            let day = calendar.date(byAdding: .day, value: -a.days, to: now)!
            return (fullDayWindow(day: day), working)
        }

        // "last <n> weeks" / "last <n> days" — a trailing window of complete
        // days up to and including today.
        if let t = findLastN(tokens) {
            strip(t.phrase)
            return (trailingDaysWindow(totalDays: t.days, now: now), working)
        }

        if working.contains(" last month ") {
            strip("last month")
            return (monthWindow(now: now, offsetMonths: -1), working)
        }
        if working.contains(" this month ") {
            strip("this month")
            return (monthWindow(now: now, offsetMonths: 0), working)
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

    private func monthWindow(now: Date, offsetMonths: Int) -> TimeWindow {
        let comps = calendar.dateComponents([.year, .month], from: now)
        let startOfThisMonth = calendar.date(from: comps)!
        let start = calendar.date(byAdding: .month, value: offsetMonths, to: startOfThisMonth)!
        let end = calendar.date(byAdding: .month, value: 1, to: start)!
        return TimeWindow(startMs: ms(start), endMs: ms(end))
    }

    /// The start-of-day for an explicit month/day, resolved to the most recent
    /// PAST occurrence relative to `now` (so a date that would fall in the future
    /// this year resolves to last year — history search never points forward).
    private func resolveExplicitDate(month: Int, day: Int, now: Date) -> Date {
        let year = calendar.component(.year, from: now)
        var comps = DateComponents()
        comps.year = year
        comps.month = month
        comps.day = day
        let date = calendar.date(from: comps)!
        if calendar.startOfDay(for: date) > now {
            comps.year = year - 1
            return calendar.date(from: comps)!
        }
        return date
    }

    private func explicitDateWindow(month: Int, day: Int, now: Date) -> TimeWindow {
        fullDayWindow(day: resolveExplicitDate(month: month, day: day, now: now))
    }

    private func dateRangeWindow(
        start: (month: Int, day: Int), end: (month: Int, day: Int), now: Date
    ) -> TimeWindow {
        let startDate = calendar.startOfDay(
            for: resolveExplicitDate(month: start.month, day: start.day, now: now)
        )
        let startYear = calendar.component(.year, from: startDate)
        var comps = DateComponents()
        comps.year = startYear
        comps.month = end.month
        comps.day = end.day
        var endDate = calendar.startOfDay(for: calendar.date(from: comps)!)
        if endDate < startDate {  // wraps a year boundary (e.g. "dec 30 to jan 2")
            comps.year = startYear + 1
            endDate = calendar.startOfDay(for: calendar.date(from: comps)!)
        }
        let endExclusive = calendar.date(byAdding: .day, value: 1, to: endDate)!
        return TimeWindow(startMs: ms(startDate), endMs: ms(endExclusive))
    }

    /// A trailing window of `totalDays` complete days ending with (and including)
    /// today. "last 2 weeks" → 14 days back through end of today.
    private func trailingDaysWindow(totalDays: Int, now: Date) -> TimeWindow {
        let startOfToday = calendar.startOfDay(for: now)
        let start = calendar.date(byAdding: .day, value: -(totalDays - 1), to: startOfToday)!
        let endExclusive = calendar.date(byAdding: .day, value: 1, to: startOfToday)!
        return TimeWindow(startMs: ms(start), endMs: ms(endExclusive))
    }

    // MARK: - Date/relative token scanners

    private static func monthNumber(_ token: String) -> Int? { months[token] }

    /// A 1–31 day number, tolerating an ordinal suffix ("3rd" → 3).
    private static func dayNumber(_ token: String) -> Int? {
        var t = token
        for suf in ["st", "nd", "rd", "th"] where t.count > 2 && t.hasSuffix(suf) {
            t = String(t.dropLast(2))
            break
        }
        guard let n = Int(t), (1...31).contains(n) else { return nil }
        return n
    }

    private static func positiveInt(_ token: String) -> Int? {
        guard let n = Int(token), n > 0 else { return nil }
        return n
    }

    private func findExplicitDate(_ tokens: [String]) -> (month: Int, day: Int, phrase: String)? {
        for (i, tok) in tokens.enumerated() {
            guard let m = Self.monthNumber(tok) else { continue }
            if i + 1 < tokens.count, let d = Self.dayNumber(tokens[i + 1]) {
                return (m, d, "\(tok) \(tokens[i + 1])")
            }
            if i >= 1, let d = Self.dayNumber(tokens[i - 1]) {
                return (m, d, "\(tokens[i - 1]) \(tok)")
            }
        }
        return nil
    }

    private func findDateRange(
        _ tokens: [String]
    ) -> (start: (month: Int, day: Int), end: (month: Int, day: Int), phrase: String)? {
        for (i, tok) in tokens.enumerated() {
            guard let m1 = Self.monthNumber(tok),
                  i + 1 < tokens.count, let d1 = Self.dayNumber(tokens[i + 1]) else { continue }
            let sep = i + 2
            guard sep < tokens.count, Self.rangeSeparators.contains(tokens[sep]) else { continue }
            let after = sep + 1
            // "<m> <d> to <m> <d>"
            if after + 1 < tokens.count,
               let m2 = Self.monthNumber(tokens[after]),
               let d2 = Self.dayNumber(tokens[after + 1]) {
                return ((m1, d1), (m2, d2), tokens[i...(after + 1)].joined(separator: " "))
            }
            // "<m> <d> to <d>" (same month)
            if after < tokens.count, let d2 = Self.dayNumber(tokens[after]) {
                return ((m1, d1), (m1, d2), tokens[i...after].joined(separator: " "))
            }
        }
        return nil
    }

    private func findDaysAgo(_ tokens: [String]) -> (days: Int, phrase: String)? {
        for (i, tok) in tokens.enumerated() where tok == "ago" {
            guard i >= 2, tokens[i - 1] == "day" || tokens[i - 1] == "days",
                  let n = Self.positiveInt(tokens[i - 2]) else { continue }
            return (n, "\(tokens[i - 2]) \(tokens[i - 1]) ago")
        }
        return nil
    }

    private func findLastN(_ tokens: [String]) -> (days: Int, phrase: String)? {
        for (i, tok) in tokens.enumerated() where tok == "last" {
            guard i + 2 < tokens.count, let n = Self.positiveInt(tokens[i + 1]) else { continue }
            let unit = tokens[i + 2]
            if unit == "weeks" || unit == "week" {
                return (n * 7, "last \(tokens[i + 1]) \(unit)")
            }
            if unit == "days" || unit == "day" {
                return (n, "last \(tokens[i + 1]) \(unit)")
            }
        }
        return nil
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
