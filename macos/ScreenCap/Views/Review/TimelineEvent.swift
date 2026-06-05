import Foundation

/// A single content field parsed from the *scrubbed* event set, classified for
/// honest rendering (U7). The distinction is load-bearing: a fail-closed token
/// must never be shown as if it were real content, and a wholesale-removed
/// field should read as protection, not as missing data.
enum RedactableField: Equatable, Hashable {
    /// The key was not present on this event.
    case absent
    /// A scrubbed-safe value to display as-is (may already contain inline
    /// anonymizer placeholders like `<EMAIL_ADDRESS>`).
    case value(String)
    /// The key was present but null — the scrubber removed the value wholesale
    /// (e.g. an excluded/masked app interval). Render as `[redacted]` (R7).
    case redacted
    /// The value was the `<SCRUB_FAILED>` sentinel — content the scrubber
    /// couldn't analyze and removed to be safe. Routes to the R14 fail-closed
    /// indicator, never displayed as content.
    case failClosed
}

/// The moment-anchored content fields the review surfaces (R5), mirroring the
/// canonical `create_html` event set (`events.py`: KeyTypeEvent.text,
/// WindowSwitchEvent.app_name/window_title/domain, AudioChunkEvent.transcription,
/// NetworkRequestEvent.host/url). Network fields are present only when the
/// uploaded event set includes them (off by default — see R5).
struct TimelineEventContent: Equatable, Hashable {
    var typedText: RedactableField = .absent
    var appName: RedactableField = .absent
    var windowTitle: RedactableField = .absent
    var domain: RedactableField = .absent
    var transcription: RedactableField = .absent
    var networkHost: RedactableField = .absent
    var networkURL: RedactableField = .absent

    /// No content field is present at all (a pure mouse/screen event).
    var isEmpty: Bool {
        [typedText, appName, windowTitle, domain, transcription, networkHost, networkURL]
            .allSatisfy { $0 == .absent }
    }

    /// Any field tripped the fail-closed sentinel.
    var hasFailClosed: Bool {
        [typedText, appName, windowTitle, domain, transcription, networkHost, networkURL]
            .contains(.failClosed)
    }
}

/// One marker on the action timeline (plan U6). Timestamps are stored as
/// seconds-from-recording-start so the timeline math works in the same space
/// as the video player's `currentTime`. The original absolute Unix timestamp
/// is preserved in `absoluteTimestamp` for any future surface that wants it.
struct TimelineEvent: Equatable, Hashable {
    let relativeSeconds: Double
    let absoluteTimestamp: Double
    let type: String
    let category: Category
    /// Moment-anchored captured content from the scrubbed event (U7). Empty for
    /// pure mouse/screen events.
    var content: TimelineEventContent = TimelineEventContent()

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
            category: TimelineEvent.Category.from(eventType: type),
            content: content(forType: type, obj: obj)
        )
    }

    /// Extract the displayable content fields for `type` from the parsed event.
    /// Only content-bearing types populate anything; everything else stays
    /// empty so pure mouse/screen events don't clutter the content view.
    private static func content(forType type: String, obj: [String: Any]) -> TimelineEventContent {
        var c = TimelineEventContent()
        switch type {
        case "key.type":
            c.typedText = field(obj, "text")
        case "window.switch", "window.state":
            c.appName = field(obj, "app_name")
            c.windowTitle = field(obj, "window_title")
            c.domain = field(obj, "domain")
        case "audio.chunk":
            c.transcription = field(obj, "transcription")
        case let t where t.hasPrefix("network."):
            // Present only when the uploaded set includes network destinations
            // (include_network is off by default — R5).
            c.networkHost = field(obj, "host")
            c.networkURL = field(obj, "url")
        default:
            break
        }
        return c
    }

    /// Classify a JSON field for honest rendering: absent key vs. a displayable
    /// value vs. a wholesale-removed (null) field vs. the fail-closed sentinel.
    private static func field(_ obj: [String: Any], _ key: String) -> RedactableField {
        guard let raw = obj[key] else { return .absent }
        if raw is NSNull { return .redacted }
        guard let s = raw as? String else { return .absent }
        if s == "<SCRUB_FAILED>" { return .failClosed }
        return .value(s)
    }
}
