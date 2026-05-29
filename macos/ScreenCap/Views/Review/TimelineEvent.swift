import Foundation

/// One marker on the action timeline (plan U6). Timestamps are stored as
/// seconds-from-recording-start so the timeline math works in the same space
/// as the video player's `currentTime`. The original absolute Unix timestamp
/// is preserved in `absoluteTimestamp` for any future surface that wants it.
struct TimelineEvent: Equatable, Hashable {
    let relativeSeconds: Double
    let absoluteTimestamp: Double
    let type: String
    let category: Category

    /// Coarse bucket the timeline uses to colorize markers. Unknown event
    /// types fall into `.other` rather than being dropped — mirrors the
    /// drift-resilient decoder pattern on the recorder-event side
    /// (`RecorderEventLine`).
    enum Category: String, Hashable {
        case mouse
        case key
        case window
        case screen
        case other

        static func from(eventType: String) -> Category {
            if eventType.hasPrefix("mouse.") { return .mouse }
            if eventType.hasPrefix("key.") { return .key }
            if eventType.hasPrefix("window.") { return .window }
            if eventType.hasPrefix("screen.") { return .screen }
            return .other
        }
    }
}

/// Parses an `events.jsonl` exported by `screencap review-data --json` (plan
/// U2). The file is JSONL: line 1 is a `_meta` header, line 2..N are
/// event objects. The decoder is tolerant — malformed lines are skipped,
/// unknown event types fall into the `.other` bucket. Parsing happens on a
/// background task at call sites that care about main-thread responsiveness.
enum TimelineEventParser {
    /// Reads `url` line-by-line and returns parsed events sorted by their
    /// recording-relative timestamp. `recordingStartedAt` is the absolute
    /// epoch seconds reported by `review-data` and is subtracted from each
    /// event's `timestamp` to produce the relative axis.
    ///
    /// Returns an empty array on read failure rather than throwing — the
    /// timeline pane renders an empty state in either case, and the parent
    /// (U8) has separate paths for hard failures via the U2 envelope. Errors
    /// here would surface as "no markers visible" which is honest.
    static func parse(url: URL, recordingStartedAt: Double) -> [TimelineEvent] {
        guard let text = try? String(contentsOf: url, encoding: .utf8) else {
            return []
        }
        return parse(jsonl: text, recordingStartedAt: recordingStartedAt)
    }

    /// String-input variant — exposed for tests and any future call site
    /// that already has the JSONL in memory.
    static func parse(jsonl: String, recordingStartedAt: Double) -> [TimelineEvent] {
        var out: [TimelineEvent] = []
        // `split` is a value-type slice walker; no extra allocation per line.
        for raw in jsonl.split(whereSeparator: \.isNewline) {
            let trimmed = raw.trimmingCharacters(in: .whitespaces)
            guard !trimmed.isEmpty, trimmed.hasPrefix("{") else { continue }
            guard let event = decodeSingle(trimmed, recordingStartedAt: recordingStartedAt) else {
                continue
            }
            out.append(event)
        }
        // Stable sort by relative timestamp. Exporter writes in chronological
        // order today, but the timeline contract should not depend on that —
        // a future re-ordering of the exporter pipeline (e.g. multi-source
        // merge) would otherwise silently break marker placement.
        out.sort { $0.relativeSeconds < $1.relativeSeconds }
        return out
    }

    private static func decodeSingle(_ line: String, recordingStartedAt: Double) -> TimelineEvent? {
        guard let data = line.data(using: .utf8) else { return nil }
        guard let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any] else {
            return nil
        }
        // The `_meta` header line carries `"_meta": true` rather than an
        // event shape. Skip it explicitly so a future schema field doesn't
        // accidentally pass the "looks like JSON" check.
        if obj["_meta"] != nil { return nil }
        guard let type = obj["type"] as? String,
              let timestamp = obj["timestamp"] as? Double
        else {
            return nil
        }
        let relative = timestamp - recordingStartedAt
        return TimelineEvent(
            relativeSeconds: max(0, relative),
            absoluteTimestamp: timestamp,
            type: type,
            category: TimelineEvent.Category.from(eventType: type)
        )
    }
}
