import Foundation

// U11 — the Clips surface's pure model layer: list grouping by source day,
// source-day + time-range formatting, the honesty-flag readout, the
// cross-recording split-range guard for range Clip/Share (KTD-8), and the
// honest copy for a clip.create failure. Factored out of the views so every rule
// is directly unit-testable (ClipsModelTests) without a render — no recording
// entity is ever surfaced (R5); clips show their SOURCE DAY + range only.

enum ClipsModel {

    // MARK: - List grouping (by source day, newest first)

    /// One day's group of clips for the Clips list — the source-day label + the
    /// clips whose `source_day` falls on it, newest clip first. `sourceDay` is the
    /// raw wire day key (`yyyy-MM-dd`) kept for a stable, order-independent id.
    struct ClipDayGroup: Identifiable, Equatable {
        let sourceDay: String
        let label: String
        let clips: [ClipRecord]
        var id: String { sourceDay }
    }

    /// Group clips by `source_day`, newest day first, newest clip first within a
    /// day (by `created_at`, then `start_ms`). Never names a recording (R5) — the
    /// group heading is the day label. An unparseable `source_day` still groups by
    /// its raw string and labels with that raw string (never dropped).
    static func groups(
        _ clips: [ClipRecord],
        now: Date = Date(),
        calendar: Calendar = .current
    ) -> [ClipDayGroup] {
        var buckets: [String: [ClipRecord]] = [:]
        for clip in clips {
            buckets[clip.sourceDay, default: []].append(clip)
        }
        return buckets
            .sorted { $0.key > $1.key }  // yyyy-MM-dd sorts chronologically as text
            .map { day, group in
                ClipDayGroup(
                    sourceDay: day,
                    label: dayLabel(day, now: now, calendar: calendar),
                    clips: group.sorted { a, b in
                        if a.createdAt != b.createdAt { return a.createdAt > b.createdAt }
                        return a.startMs > b.startMs
                    }
                )
            }
    }

    // MARK: - Source-day + range formatting

    /// Parse a `yyyy-MM-dd` wire day into a local start-of-day `Date`, or nil when
    /// unparseable (a malformed / future-shaped key).
    static func parseSourceDay(_ sourceDay: String, calendar: Calendar = .current) -> Date? {
        let f = DateFormatter()
        f.locale = Locale(identifier: "en_US_POSIX")
        f.calendar = calendar
        f.timeZone = calendar.timeZone
        f.dateFormat = "yyyy-MM-dd"
        guard let date = f.date(from: sourceDay) else { return nil }
        return calendar.startOfDay(for: date)
    }

    /// The source-day heading: Today / Yesterday / "Wednesday, 16 July" (+ year
    /// when not this year). Falls back to the raw wire key when it can't be parsed,
    /// so a clip is never mislabelled or hidden.
    static func dayLabel(
        _ sourceDay: String,
        now: Date = Date(),
        calendar: Calendar = .current
    ) -> String {
        guard let day = parseSourceDay(sourceDay, calendar: calendar) else { return sourceDay }
        return DaysModel.label(for: day, now: now, calendar: calendar)
    }

    /// "HH:mm–HH:mm" over the clip's absolute-ms range — what the Clips row shows
    /// instead of any recording name (R5).
    static func rangeClockText(_ clip: ClipRecord) -> String {
        "\(clock(clip.startMs))–\(clock(clip.endMs))"
    }

    /// "m:ss" duration readout for the clip's span (never negative).
    static func durationText(_ clip: ClipRecord) -> String {
        let totalSeconds = max(0, clip.endMs - clip.startMs) / 1000
        return String(format: "%d:%02d", totalSeconds / 60, totalSeconds % 60)
    }

    /// The combined accessible/summary line for a clip row: source day + range +
    /// duration, and the policy-purged-partial flag note when present.
    static func summaryLine(_ clip: ClipRecord, now: Date = Date(), calendar: Calendar = .current) -> String {
        var parts = ["\(dayLabel(clip.sourceDay, now: now, calendar: calendar)) · \(rangeClockText(clip)) · \(durationText(clip))"]
        if clip.honestyFlags.policyPurgedPartial {
            parts.append(policyPurgedFlagText)
        }
        return parts.joined(separator: " — ")
    }

    // MARK: - Honesty flags (R17 / AE8)

    /// The badge copy for a clip a retroactive privacy purge partially overlapped
    /// (`policy_purged_partial`). The clip is KEPT and its mp4 is NOT re-cut, so
    /// the overlapping footage the privacy rule removed from your library still
    /// lives in this clip — the copy says exactly that so a user can't share it
    /// believing the purged part is gone (the removal happened elsewhere, not in
    /// the clip). See SECURITY.md (clips-store remanence).
    static let policyPurgedFlagText =
        "This clip still contains footage your privacy rules later removed from your library"

    /// Whether a clip carries the agent-created attribution (creator == mcp) —
    /// surfaced so an agent-made clip stays attributable (R18).
    static func isAgentCreated(_ clip: ClipRecord) -> Bool {
        clip.creator == "mcp"
    }

    // MARK: - Cross-recording split-range guard (KTD-8)

    /// A recording's day-clamped footage track (absolute unix ms) — the input to
    /// the single-recording clip/share resolution.
    struct RecordingTrack: Equatable {
        let recording: String
        let startMs: Int
        let endMs: Int
    }

    /// The outcome of resolving a selected range to a single clippable recording.
    enum RangeResolution: Equatable {
        /// The range sits entirely within one recording — clip/share proceed.
        case single(recording: String)
        /// The range crosses a recording boundary — clip/share are single-recording
        /// in v1 (KTD-8); `splitMs` names where the footage switches recordings.
        case crossesRecordings(splitMs: Int)
        /// The range covers no footage at all (a pure gap) — nothing to clip.
        case noFootage
    }

    /// Resolve `[startMs, endMs)` against the day's base tracks (KTD-8). One
    /// overlapping recording → `.single`; two or more → `.crossesRecordings` naming
    /// the split (the first overlapping recording's end within the range); none →
    /// `.noFootage`. Half-open intersection so a range abutting a boundary doesn't
    /// spuriously read as crossing.
    static func resolveRange(
        startMs: Int,
        endMs: Int,
        tracks: [RecordingTrack]
    ) -> RangeResolution {
        let overlapping = tracks
            .filter { $0.startMs < endMs && $0.endMs > startMs }
            .sorted { $0.startMs < $1.startMs }
        switch overlapping.count {
        case 0:
            return .noFootage
        case 1:
            return .single(recording: overlapping[0].recording)
        default:
            // The footage switches recordings at the first track's end (clamped
            // into the selected range so the named instant is inside the selection).
            let split = min(max(overlapping[0].endMs, startMs), endMs)
            return .crossesRecordings(splitMs: split)
        }
    }

    // MARK: - Failure copy (clip.create)

    /// Honest copy for the cross-recording guard (KTD-8): the range spans two
    /// recordings, so it can't be a single clip in v1 — name the split instant.
    static func crossRecordingMessage(splitMs: Int) -> String {
        "That range crosses a recording boundary at \(clock(splitMs)). Clips and shares cover a single recording — select a range within one recording."
    }

    /// Honest copy for a range that covers no footage (a pure gap).
    static let noFootageMessage =
        "There's no footage in that range to clip. Select a range over recorded footage."

    /// Map a `clip.create` clip-domain `reason` (envelope `ok=false`) to plain copy.
    static func reasonMessage(_ reason: String) -> String {
        switch reason {
        case "policy_purged":
            return "Part of that range was removed by your privacy rules, so it can't be clipped."
        case "not_eligible":
            return "That footage can't be clipped."
        case "no_frames_in_range":
            return "There are no video frames in that range to clip."
        case "masked_video_required":
            return "Clipping this footage needs masked-video export, which isn't enabled."
        case "clip_busy":
            return "Another clip is being made right now. Try again in a moment."
        default:
            return "Couldn't make that clip (\(reason))."
        }
    }

    /// Map a thrown `DaemonClientError` from a clip verb (sealed store, bad range,
    /// daemon down) to plain copy.
    static func errorMessage(_ error: Error) -> String {
        if case let DaemonClientError.envelopeError(code, _) = error {
            switch code {
            case "store_locked":
                return "Your storage is locked. Unlock it to make or manage clips."
            case "store_absent":
                return "Your storage isn't set up yet, so there's nothing to clip."
            case "invalid_request":
                return "That range isn't valid to clip."
            default:
                return "Couldn't make that clip (\(code))."
            }
        }
        return "Couldn't make that clip. \(error.localizedDescription)"
    }

    // MARK: - Formatting

    private static let clockFormatter: DateFormatter = {
        let f = DateFormatter()
        f.dateFormat = "HH:mm"
        return f
    }()

    static func clock(_ ms: Int) -> String {
        clockFormatter.string(from: Date(timeIntervalSince1970: Double(ms) / 1000))
    }
}
