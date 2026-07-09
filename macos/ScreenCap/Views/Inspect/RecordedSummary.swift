import Foundation

/// A half-open `[startMs, endMs]` interval in absolute Unix epoch milliseconds,
/// as emitted by `inspect-data`'s `blocked_intervals` / `protected_intervals`
/// (review schema v4). Decodes the Python `{start_ms, end_ms}` shape directly,
/// and is the same coordinate space `DayStripAccessibility.hourMinuteText(ms:)`
/// renders from (`Date(timeIntervalSince1970: ms / 1000)`).
struct CapturedInterval: Decodable, Equatable {
    let startMs: Int
    let endMs: Int

    enum CodingKeys: String, CodingKey {
        case startMs = "start_ms"
        case endMs = "end_ms"
    }

    init(startMs: Int, endMs: Int) {
        self.startMs = startMs
        self.endMs = endMs
    }

    /// Whether an absolute epoch-millisecond instant lies within this interval.
    /// Inclusive on both ends: an event landing exactly on a boundary is dropped,
    /// keeping the digest fail-closed — it must never count or reveal content the
    /// policy flagged, mirroring the ALLOW-only discipline of
    /// `content_index` / `backfill` / `frame.nearest`.
    func contains(ms: Int) -> Bool { ms >= startMs && ms <= endMs }

    var durationMs: Int { max(0, endMs - startMs) }
}

/// The friendly, reassurance-oriented grouping of the *meaningful* captured
/// events the digest counts (KTD4). Raw low-level input — `mouse.move`,
/// `mouse.down`/`up`, `mouse.scroll`, `key.down`/`up`, `screen.frame`,
/// `audio.chunk` — is NOT meaningful and maps to `nil`, so it never reaches the
/// digest. `mouse.singleclick` is the emitted click type (`mouse.click` is never
/// emitted — see `EVENT_TYPE_MAP` in `src/screencap/engine/events.py`). Cases are
/// declared in display order.
enum MeaningfulEventKind: String, CaseIterable {
    case clicks
    case typedText
    case shortcuts
    case windowSwitches
    case networkRequests

    /// Friendly label rendered in the digest — never the raw dotted type string.
    var label: String {
        switch self {
        case .clicks: return "Clicks"
        case .typedText: return "Typed text"
        case .shortcuts: return "Keyboard shortcuts"
        case .windowSwitches: return "App & window switches"
        case .networkRequests: return "Network requests"
        }
    }

    /// Map an emitted event `type` to its meaningful kind, or `nil` for raw
    /// low-level input the digest excludes. Pinned to the engine's `EventType`
    /// vocabulary (`src/screencap/engine/events.py`).
    static func from(eventType: String) -> MeaningfulEventKind? {
        switch eventType {
        case "mouse.singleclick", "mouse.doubleclick", "mouse.drag":
            return .clicks
        case "key.type":
            return .typedText
        case "key.shortcut", "key.special":
            return .shortcuts
        case "window.switch", "window.state":
            return .windowSwitches
        case "network.request", "network.response":
            return .networkRequests
        default:
            return nil
        }
    }
}

/// One friendly-kind count line in the digest.
struct MeaningfulKindCount: Equatable, Identifiable {
    let kind: MeaningfulEventKind
    let count: Int
    var id: String { kind.rawValue }
    var label: String { kind.label }
}

/// The meaningful events captured while a single app was frontmost, with a
/// per-kind breakdown and the underlying events for drill-down (R6). `isUnknown`
/// marks the pre-first-window-switch bucket, pinned last in the digest.
struct RecordedAppSummary: Equatable, Identifiable {
    /// Display name; the unknown bucket carries a friendly placeholder here.
    let appName: String
    let isUnknown: Bool
    let totalCount: Int
    /// Ordered, non-zero-only per-kind counts for this app.
    let kindCounts: [MeaningfulKindCount]
    /// The underlying meaningful events (time-sorted) this group drills into.
    let events: [TimelineEvent]

    /// A NUL-sentinel id for the unknown bucket so it can never collide with a
    /// real app that happens to share the placeholder display name.
    var id: String { isUnknown ? "\u{0}unknown\u{0}" : appName }
}

/// The digest a `RecordedSummaryPane` renders: per-app groups, overall per-kind
/// counts, and the excluded-only blocked-interval reassurance data. Built purely
/// from the completed `TimelineEvent` array (KTD1) plus the payload's two
/// interval sets — recomputed whenever the event array changes, never gated on
/// the window's `ready` transition.
struct RecordedSummary: Equatable {
    /// Per-app groups, ordered by descending meaningful-event count with the
    /// unknown bucket pinned last.
    let apps: [RecordedAppSummary]
    /// Overall meaningful-event counts by kind — ordered, non-zero only (R3).
    let kindCounts: [MeaningfulKindCount]
    /// Total meaningful events counted after the protected-interval skip.
    let totalMeaningfulCount: Int
    /// Capture-time-**excluded**-only intervals (the narrower set — KTD3), merged
    /// and start-sorted, backing the "provably not captured" reassurance line.
    /// Never the full protected set, which would misreport masked-but-captured
    /// apps as "not captured".
    let blockedIntervals: [CapturedInterval]

    /// No meaningful events survived the protected-interval skip (R7 / AE5). A
    /// recording can be meaningful-empty while still carrying blocked intervals.
    var isEmpty: Bool { totalMeaningfulCount == 0 }

    var blockedCount: Int { blockedIntervals.count }
    /// Total not-captured span (ms) across the already-merged blocked set.
    var blockedTotalMs: Int { blockedIntervals.reduce(0) { $0 + $1.durationMs } }
    var hasBlocked: Bool { !blockedIntervals.isEmpty }

    static let empty = RecordedSummary(
        apps: [], kindCounts: [], totalMeaningfulCount: 0, blockedIntervals: []
    )
}

/// Pure builder for the recorded-events digest (KTD1/KTD4). One O(N) pass over
/// the time-sorted event stream, carrying the current frontmost app forward.
/// Namespaced as an enum to mirror `EventContent` / `TimelineEventParser` /
/// `SearchRanking`.
enum RecordedSummaryBuilder {
    /// Fold `events` into a `RecordedSummary`.
    ///
    /// - `events`: the completed, time-sorted `TimelineEvent` array (the parser
    ///   guarantees the sort). Each event carries an absolute epoch-**seconds**
    ///   timestamp.
    /// - `blockedIntervals`: capture-time-excluded-only spans (epoch **ms**) →
    ///   the "not captured" reassurance line.
    /// - `protectedIntervals`: the full flagged set (epoch **ms**) → any event
    ///   whose timestamp falls inside one is dropped before counting/grouping, so
    ///   the digest never counts or reveals policy-flagged content (KTD4).
    static func build(
        events: [TimelineEvent],
        blockedIntervals: [CapturedInterval],
        protectedIntervals: [CapturedInterval]
    ) -> RecordedSummary {
        // First-seen app order plus per-app accumulators. `currentApp == nil`
        // routes to the pre-first-switch "unknown" bucket.
        var order: [String] = []
        var perAppEvents: [String: [TimelineEvent]] = [:]
        var perAppKindCounts: [String: [MeaningfulEventKind: Int]] = [:]
        var unknownEvents: [TimelineEvent] = []
        var unknownKindCounts: [MeaningfulEventKind: Int] = [:]
        var overall: [MeaningfulEventKind: Int] = [:]

        var currentApp: String?

        for event in events {
            // Advance the running frontmost app on any window event that names
            // one — BEFORE attributing this event, so a switch counts under the
            // app it switches TO. Only a `.value` name counts; a scrubbed/absent
            // name leaves the previous app in force (defensive; inspect is
            // unmasked so this is rarely hit).
            if event.category == .window, case .value(let name) = event.content.appName {
                currentApp = name
            }

            guard let kind = MeaningfulEventKind.from(eventType: event.type) else {
                continue  // raw low-level input — never in the digest
            }

            // Drop anything inside a protected interval. Events carry epoch
            // SECONDS; intervals are epoch MS.
            let ms = Int((event.absoluteTimestamp * 1000).rounded())
            if protectedIntervals.contains(where: { $0.contains(ms: ms) }) {
                continue
            }

            overall[kind, default: 0] += 1

            if let app = currentApp {
                if perAppEvents[app] == nil {
                    perAppEvents[app] = []
                    perAppKindCounts[app] = [:]
                    order.append(app)
                }
                perAppEvents[app]?.append(event)
                perAppKindCounts[app]?[kind, default: 0] += 1
            } else {
                unknownEvents.append(event)
                unknownKindCounts[kind, default: 0] += 1
            }
        }

        // Deterministic order: descending total, ties broken by first-seen order.
        let firstSeen = Dictionary(
            uniqueKeysWithValues: order.enumerated().map { ($1, $0) }
        )
        var apps: [RecordedAppSummary] = order.map { app in
            RecordedAppSummary(
                appName: app,
                isUnknown: false,
                totalCount: perAppEvents[app]?.count ?? 0,
                kindCounts: orderedCounts(perAppKindCounts[app] ?? [:]),
                events: perAppEvents[app] ?? []
            )
        }
        apps.sort {
            if $0.totalCount != $1.totalCount { return $0.totalCount > $1.totalCount }
            return (firstSeen[$0.appName] ?? 0) < (firstSeen[$1.appName] ?? 0)
        }
        if !unknownEvents.isEmpty {
            apps.append(RecordedAppSummary(
                appName: "Unknown app",
                isUnknown: true,
                totalCount: unknownEvents.count,
                kindCounts: orderedCounts(unknownKindCounts),
                events: unknownEvents
            ))
        }

        return RecordedSummary(
            apps: apps,
            kindCounts: orderedCounts(overall),
            totalMeaningfulCount: overall.values.reduce(0, +),
            blockedIntervals: blockedIntervals.sorted { $0.startMs < $1.startMs }
        )
    }

    /// Project a kind→count map into the stable display order, non-zero only.
    private static func orderedCounts(
        _ counts: [MeaningfulEventKind: Int]
    ) -> [MeaningfulKindCount] {
        MeaningfulEventKind.allCases.compactMap { kind in
            guard let c = counts[kind], c > 0 else { return nil }
            return MeaningfulKindCount(kind: kind, count: c)
        }
    }
}
