import SwiftUI

// U9/U10 — the horizontal day strip (design 444–467): agent/user task bands
// (SCR-214) overlaid on each recording's "unsplit — still searchable" base
// track, over a neutral "nothing captured" track, with hatched provably-blocked
// bands, amber search-match markers, a black playhead, hour labels, and the
// legend. The geometry lives in the pure `DayStripLayout` (adapted from the
// retired SearchTimelineLayout) so time→x mapping, the dynamic axis bounds, and
// the "nothing captured" gap complement are unit-testable without a render
// (DayStripLayoutTests / DayTimelineTaskBandsTests).

/// A recording's full day-clamped span (from `/v0/timeline.day`), drawn as the
/// "unsplit — still searchable" base layer. Task bands overlay it; wherever no
/// task covers, the base shows through as unsplit-but-searchable footage
/// (ambient capture, SCR-214). Keyed on `recording` — one base per recording.
struct DayStripBaseTrack: Equatable, Identifiable {
    let recording: String
    let title: String
    let startMs: Int
    let endMs: Int
    var id: String { recording }
}

/// One agent/user task band (SCR-214) from a recording's `tasks[]`, overlaid on
/// its base track with a label. Task spans are a **non-overlapping partition**
/// (enforced by U6's carve-out + U7's overlap validation), so bands need no
/// stacking / z-order. Labeled with the task *name* (not the recording title).
struct DayStripSegment: Equatable, Identifiable {
    let recording: String
    let taskIndex: Int
    let name: String
    let category: String?
    let startMs: Int
    let endMs: Int

    /// Task-stable **and** globally unique on the strip: `task_index` is unique
    /// per recording (ledger `UNIQUE (recording_id, task_index)`), so the
    /// `recording` name + index pair never collides across a day's recordings.
    var id: String { "\(recording)#\(taskIndex)" }

    /// Build the day's task bands from `timeline.day` recordings: N bands per
    /// recording from its `tasks[]`. Task `start_ts`/`end_ts` are Unix *seconds*
    /// (the ledger's native units) → scaled to the strip's ms coordinate space
    /// (the same space as `start_ms`/`end_ms`). A recording with no tasks
    /// contributes nothing (it renders as a plain unsplit base band). Mirrors
    /// `DayStripBlockedBand.provenBands(from:)` so the mapping is render-free
    /// unit-testable.
    static func bands(from spans: [DaySegmentRecording]) -> [DayStripSegment] {
        spans.flatMap { span in
            span.tasks.map { task in
                DayStripSegment(
                    recording: span.name,
                    taskIndex: task.taskIndex,
                    name: task.name,
                    category: task.category,
                    startMs: Int((task.startTs * 1000).rounded()),
                    endMs: Int((task.endTs * 1000).rounded())
                )
            }
        }
    }
}

/// A provably-blocked band (`blocked_proven` only — `unverifiable` intervals
/// and plain gaps render neutrally with no claim, R7).
struct DayStripBlockedBand: Equatable {
    let startMs: Int
    let endMs: Int

    /// The only path from `/v0/timeline.day` intervals to hatched bands: it
    /// reads `blockedProven` exclusively, so an `unverifiable` interval can
    /// never acquire the "blocked" label (R7 — pinned in DayStripLayoutTests).
    static func provenBands(from spans: [DaySegmentRecording]) -> [DayStripBlockedBand] {
        spans.flatMap { span in
            span.blockedProven.map { DayStripBlockedBand(startMs: $0.startMs, endMs: $0.endMs) }
        }
    }
}

/// U5 — the purged-band mapping: a straight union of every recording's
/// retroactively-purged intervals (identity rides along for the R6 hover copy).
/// Purged renders as its own class — the blocked hatch *geometry* in a distinct
/// plum hue (R10) — never the blocked rust.
extension DayPurgedInterval {
    static func bands(from spans: [DaySegmentRecording]) -> [DayPurgedInterval] {
        spans.flatMap(\.purged)
    }
}

/// Pure axis geometry for the day strip.
enum DayStripLayout {

    struct Bounds: Equatable {
        let startMs: Int
        let endMs: Int
        var spanMs: Int { endMs - startMs }
    }

    static let hourMs = 3_600_000
    /// The fixed waking window the axis always spans, as hour offsets from
    /// local midnight: 08:00–21:00.
    static let wakeStartHour = 8
    static let wakeEndHour = 21

    /// Honest full-day axis bounds (U1, R6): the axis always spans the fixed
    /// 08:00–21:00 waking window, extended *outward* (hour-rounded) only when
    /// footage falls outside it, and clamped into the real day via the passed
    /// `dayEndMs` (DST-safe). Short footage renders as a small band on this
    /// honest axis rather than being zoomed to fill — a sparse day reads as
    /// "not recording", never "lost". A spanless day still shows the waking
    /// window. Replaces the old footage-union + 8h-floor rule that made a
    /// 51-minute recording look like "a few minutes".
    static func axisBounds(dayStartMs: Int, dayEndMs: Int, spans: [(startMs: Int, endMs: Int)]) -> Bounds {
        func floorHour(_ ms: Int) -> Int {
            dayStartMs + ((ms - dayStartMs) / hourMs) * hourMs
        }
        func ceilHour(_ ms: Int) -> Int {
            let offset = ms - dayStartMs
            let rounded = (offset + hourMs - 1) / hourMs * hourMs
            return dayStartMs + rounded
        }
        var start = dayStartMs + wakeStartHour * hourMs
        var end = dayStartMs + wakeEndHour * hourMs
        if let lo = spans.map(\.startMs).min(), lo < start {
            start = floorHour(max(lo, dayStartMs))
        }
        if let hi = spans.map(\.endMs).max(), hi > end {
            end = ceilHour(min(hi, dayEndMs))
        }
        start = max(dayStartMs, start)
        end = min(dayEndMs, end)
        if end <= start { end = min(dayEndMs, start + hourMs) }  // never invert (DST/clock skew)
        return Bounds(startMs: start, endMs: end)
    }

    /// An honest edge label at a footage boundary (AE1): "recording started"
    /// at the earliest span start, "recording stopped" at the latest span end.
    struct BoundaryLabel: Equatable {
        enum Kind: Equatable { case started, stopped }
        let ms: Int
        let kind: Kind
    }

    /// The two footage-edge labels for a day's spans — the first start and the
    /// last end — so the strip can mark where footage begins and ends on the
    /// full-day axis. A spanless day has none.
    static func footageBoundaryLabels(spans: [(startMs: Int, endMs: Int)]) -> [BoundaryLabel] {
        guard let lo = spans.map(\.startMs).min(),
              let hi = spans.map(\.endMs).max()
        else { return [] }
        return [BoundaryLabel(ms: lo, kind: .started), BoundaryLabel(ms: hi, kind: .stopped)]
    }

    /// Resolve a landing highlight (a Tasks/Chat jump to a task span, AE3) to the
    /// exact span the strip should emphasize. Precedence: a task band with
    /// matching endpoints, then the task band containing the highlight's
    /// midpoint, else the raw highlight span — so a highlight always renders,
    /// even before the task bands load or when it lands in unsplit footage.
    /// Pure so the emphasis geometry is unit-testable without a render.
    static func resolveHighlightSpan(
        highlight: (startMs: Int, endMs: Int),
        segments: [(startMs: Int, endMs: Int)]
    ) -> (startMs: Int, endMs: Int) {
        if let exact = segments.first(where: {
            $0.startMs == highlight.startMs && $0.endMs == highlight.endMs
        }) {
            return exact
        }
        let mid = highlight.startMs + (highlight.endMs - highlight.startMs) / 2
        if let containing = segments.first(where: { $0.startMs <= mid && mid < $0.endMs }) {
            return containing
        }
        return highlight
    }

    /// Map a wall-clock ms to an x in `[0, width]`, clamped to the bounds. A
    /// non-positive width or span maps everything to 0 — no NaN, no
    /// divide-by-zero (the retired SearchTimelineLayout's placeMarkers rule).
    static func x(forMs ms: Int, bounds: Bounds, width: CGFloat) -> CGFloat {
        let span = Double(bounds.spanMs)
        guard width > 0, span > 0 else { return 0 }
        let ratio = min(1, max(0, Double(ms - bounds.startMs) / span))
        return CGFloat(ratio) * width
    }

    /// The inverse mapping — a tapped x back to a wall-clock ms.
    static func ms(forX x: CGFloat, bounds: Bounds, width: CGFloat) -> Int {
        guard width > 0, bounds.spanMs > 0 else { return bounds.startMs }
        let ratio = min(1, max(0, Double(x / width)))
        return bounds.startMs + Int(ratio * Double(bounds.spanMs))
    }

    /// Hour tick positions for the label row: every 2h up to a 12h span, every
    /// 4h beyond (keeps the row legible on a full 24h axis).
    static func hourTicks(bounds: Bounds) -> [Int] {
        let stepHours = bounds.spanMs <= 12 * hourMs ? 2 : 4
        let step = stepHours * hourMs
        return stride(from: bounds.startMs, through: bounds.endMs, by: step).map { $0 }
    }

    /// Collapse marker xs by single-linkage chaining (the retired
    /// SearchTimelineLayout's clustering contract, same 14pt default
    /// threshold): walking left-to-right,
    /// an x joins the current cluster when within `thresholdPx` of the previous
    /// one; the cluster renders at the mean x. Keeps a dense day's markers
    /// legible instead of painting dozens of overlapping bars.
    static func clusterXs(_ xs: [CGFloat], thresholdPx: CGFloat = 14) -> [CGFloat] {
        captionClusters(xs.map { (x: $0, cls: .blocked) }, thresholdPx: thresholdPx).map(\.x)
    }

    /// A placed task label above its band (design 451–453). `truncated` is true
    /// exactly when the measured name doesn't fit the allowed width, so the
    /// draw site knows to render an ellipsized string instead of hard-clipping
    /// mid-glyph (the full name stays reachable via the overlay's `.help` +
    /// accessibility label).
    struct TaskLabelFrame: Equatable {
        let minX: CGFloat
        let width: CGFloat
        let truncated: Bool
    }

    /// Greedy left-to-right task-label placement over the sorted bands: a label
    /// is skipped (nil, position-aligned with the input) when its band starts
    /// within `spacingPx` of the previous placed label's end — adjacent narrow
    /// tasks would otherwise paint on top of each other. A placed label gets
    /// `min(measured, max(bandWidth, minHintWidth))`: it truncates to the band
    /// but never below the 44pt one-word hint, so a narrow band's label shrinks
    /// rather than disappears. Bands must be sorted by `minX`.
    static func taskLabelFrames(
        bands: [(minX: CGFloat, width: CGFloat)],
        measuredWidths: [CGFloat],
        minHintWidth: CGFloat = 44,
        spacingPx: CGFloat = 8
    ) -> [TaskLabelFrame?] {
        var frames: [TaskLabelFrame?] = []
        var lastLabelEndX = -CGFloat.greatestFiniteMagnitude
        for (band, measured) in zip(bands, measuredWidths) {
            guard band.minX >= lastLabelEndX + spacingPx else {
                frames.append(nil)
                continue
            }
            let width = min(measured, max(band.width, minHintWidth))
            frames.append(TaskLabelFrame(minX: band.minX, width: width, truncated: measured > width))
            lastLabelEndX = band.minX + width
        }
        return frames
    }

    /// A caption band's class. Both `blocked` and `purged` (retroactively
    /// purged app spans) render today — `captionClusters` is fed both band
    /// sets. A mixed cluster keeps the "blocked" wording (the hard-stop claim
    /// dominates the shared slot); the class tag exists so the single
    /// overlap guard covers both classes in one pass.
    enum CaptionClass: Hashable {
        case blocked
        case purged
    }

    /// One caption per single-linkage cluster: the cluster's mean leading x +
    /// the union of the member bands' classes.
    struct Caption: Equatable {
        let x: CGFloat
        let classes: Set<CaptionClass>
    }

    /// Collapse per-band captions to one per cluster by single-linkage chaining
    /// (the `clusterXs` contract; the default threshold is the 50pt caption
    /// width). The guard runs across ALL caption classes in one pass, so two
    /// nearby blocked bands — or a blocked band next to a purged one — coalesce
    /// into a single mixed-class caption instead of overprinting.
    static func captionClusters(
        _ bands: [(x: CGFloat, cls: CaptionClass)],
        thresholdPx: CGFloat = 50
    ) -> [Caption] {
        let sorted = bands.sorted { $0.x < $1.x }
        guard let first = sorted.first else { return [] }
        guard thresholdPx > 0 else { return sorted.map { Caption(x: $0.x, classes: [$0.cls]) } }
        var clusters: [Caption] = []
        var bucketXs: [CGFloat] = [first.x]
        var bucketClasses: Set<CaptionClass> = [first.cls]
        func flush() {
            clusters.append(
                Caption(x: bucketXs.reduce(0, +) / CGFloat(bucketXs.count), classes: bucketClasses)
            )
        }
        for band in sorted.dropFirst() {
            if band.x - bucketXs[bucketXs.count - 1] <= thresholdPx {
                bucketXs.append(band.x)
                bucketClasses.insert(band.cls)
            } else {
                flush()
                bucketXs = [band.x]
                bucketClasses = [band.cls]
            }
        }
        flush()
        return clusters
    }

    /// An hour-tick label's leading x: centered on its tick from the *measured*
    /// label width (the old fixed -14pt offset assumed one width and let the
    /// edge labels spill outside the strip), with the first/last labels clamped
    /// inside `[0, stripWidth]`.
    static func tickLabelMinX(tickX: CGFloat, labelWidth: CGFloat, stripWidth: CGFloat) -> CGFloat {
        let centered = tickX - labelWidth / 2
        return min(max(0, centered), max(0, stripWidth - labelWidth))
    }

    /// The "nothing captured" complement: the axis span minus the union of the
    /// day's recording base tracks. These are the between-recording gaps where
    /// the neutral track shows through with no footage (R7) — announced to
    /// VoiceOver so every region type is reachable. Overlapping / unsorted spans
    /// are merged first, and each gap is clamped into the bounds.
    static func gaps(bounds: Bounds, spans: [(startMs: Int, endMs: Int)]) -> [(startMs: Int, endMs: Int)] {
        guard bounds.spanMs > 0 else { return [] }
        // Clamp spans into the axis, drop empties, sort, then merge overlaps.
        let clamped = spans
            .map { (startMs: max($0.startMs, bounds.startMs), endMs: min($0.endMs, bounds.endMs)) }
            .filter { $0.endMs > $0.startMs }
            .sorted { $0.startMs < $1.startMs }
        var merged: [(startMs: Int, endMs: Int)] = []
        for span in clamped {
            if let last = merged.last, span.startMs <= last.endMs {
                merged[merged.count - 1].endMs = max(last.endMs, span.endMs)
            } else {
                merged.append(span)
            }
        }
        var result: [(startMs: Int, endMs: Int)] = []
        var cursor = bounds.startMs
        for span in merged {
            if span.startMs > cursor {
                result.append((startMs: cursor, endMs: span.startMs))
            }
            cursor = max(cursor, span.endMs)
        }
        if cursor < bounds.endMs {
            result.append((startMs: cursor, endMs: bounds.endMs))
        }
        return result
    }

    // MARK: - U5: gap → cause provenance

    /// A recording span with its end-status provenance (`timeline.day` v3) —
    /// the resolver input. `endStatus` is the open wire string
    /// ("live"|"clean"|"interrupted"|"unknown"); nil (an older daemon) means
    /// unknown provenance.
    struct SpanProvenance: Equatable {
        let startMs: Int
        let endMs: Int
        let endStatus: String?
    }

    /// Why an empty stretch is empty — exactly one cause per gap (R4), each a
    /// claim the wire can prove (R7). `.none` claims nothing: future time, or
    /// the day query hasn't returned yet (the load gate).
    enum GapCause: Equatable {
        case nothingOnFile
        case interrupted(aroundMs: Int)
        case stillRecording
        case cantVerify
        case none
    }

    /// Resolve one gap to its single honest cause. Precedence: the load gate
    /// (no data → no claim), then the future (claims NOTHING, R11), then the
    /// store gate (sealed vault → nothing is verifiable), then the *preceding
    /// same-day recording's* end status — interruption attribution never
    /// crosses the day because `spans` is the requested day's (day-clamped)
    /// set. A gap with no same-day predecessor is an honest data claim: the
    /// query returned and holds nothing there. Unknown / nil / unrecognized
    /// statuses are unprovable → "can't verify", never a fabricated cause (R7);
    /// a live predecessor reads "still recording" and is never called
    /// interrupted (R11). Callers must pre-split gaps at `nowMs`
    /// (`splitGapAtNow`) so one gap never mixes a past claim with future time.
    ///
    /// Coverage gate (`coverageComplete == false`): a recording with an
    /// unreadable `recording.db` couldn't be placed on the day, so an empty
    /// stretch is no longer proof nothing is on file — the confident
    /// `.nothingOnFile` resolutions degrade to `.cantVerify`. Interrupted /
    /// still-recording / future / no-claim behavior is unchanged: those claims
    /// rest on a *placed* recording's own evidence, not on the gap being empty.
    static func gapCause(
        gapStartMs: Int,
        gapEndMs: Int,
        spans: [SpanProvenance],
        storeMounted: Bool,
        coverageComplete: Bool = true,
        provenanceReady: Bool,
        nowMs: Int
    ) -> GapCause {
        guard provenanceReady else { return .none }
        guard gapStartMs < nowMs else { return .none }
        guard storeMounted else { return .cantVerify }
        let nothingOnFile: GapCause = coverageComplete ? .nothingOnFile : .cantVerify
        let preceding = spans
            .filter { $0.endMs <= gapStartMs }
            .max { $0.endMs < $1.endMs }
        guard let preceding else { return nothingOnFile }
        switch preceding.endStatus {
        case "clean": return nothingOnFile
        case "interrupted": return .interrupted(aroundMs: preceding.endMs)
        case "live": return .stillRecording
        default: return .cantVerify
        }
    }

    /// Split a gap at `nowMs` so the pre-now part can carry a cause while the
    /// future part claims nothing (R11 — "still recording" is bounded to now).
    /// Fully-past and fully-future gaps pass through unsplit.
    static func splitGapAtNow(startMs: Int, endMs: Int, nowMs: Int) -> [(startMs: Int, endMs: Int)] {
        guard startMs < nowMs, nowMs < endMs else { return [(startMs: startMs, endMs: endMs)] }
        return [(startMs: startMs, endMs: nowMs), (startMs: nowMs, endMs: endMs)]
    }

    // MARK: - U5: hover hit targets (R13)

    /// An overlay's hover/accessibility frame: `index` points back into the
    /// caller's region array; the frame is the region's, expanded to the
    /// minimum hit-target width when the region is a sliver.
    struct HitFrame: Equatable {
        let index: Int
        let minX: CGFloat
        let width: CGFloat
    }

    /// Every drawn region must be hover-targetable (R13): expand slivers
    /// (centered) to `minWidth`, and return the frames in back-to-front draw
    /// order — wider originals first — so where expanded frames collide the
    /// SMALLER region renders later, sits on top, and wins the hover (later
    /// ZStack children win hit-testing in SwiftUI). Ties keep input order.
    static func hitFrames(
        _ regions: [(minX: CGFloat, width: CGFloat)],
        minWidth: CGFloat = 6
    ) -> [HitFrame] {
        regions.enumerated()
            .map { index, region -> HitFrame in
                let width = max(region.width, minWidth)
                return HitFrame(index: index, minX: region.minX - (width - region.width) / 2, width: width)
            }
            .sorted { a, b in
                let (wa, wb) = (regions[a.index].width, regions[b.index].width)
                return wa == wb ? a.index < b.index : wa > wb
            }
    }
}

/// The strip legend's honest copy + swatch kinds — U6: one plain-language
/// vocabulary a first-time user can parse without product terms (R8), shared
/// with the accessibility labels and the recorded-summary pane (R9). The
/// empty-stretch entry points at hover for the per-gap cause; the purged entry
/// has its OWN swatch (plum, never the blocked rust — R10) and never pairs
/// "blocked"/"nothing captured" with a purge (AE10). Pure data so the entry
/// set is unit-testable without a render (the `DayStripAccessibility` pattern).
enum DayStripLegend {
    enum Swatch: Equatable {
        case task
        case unsplit
        case nothingCaptured
        case searchMatch
        case blocked
        case purged
    }

    struct Item: Equatable, Identifiable {
        let text: String
        let swatch: Swatch
        var id: String { text }
    }

    static let items: [Item] = [
        Item(text: "task", swatch: .task),
        Item(text: "recorded — searchable", swatch: .unsplit),
        Item(text: "empty — hover for why", swatch: .nothingCaptured),
        Item(text: "search match", swatch: .searchMatch),
        Item(text: "blocked at capture", swatch: .blocked),
        Item(text: "removed by your rules", swatch: .purged),
    ]
}

/// U5 — the purged-hatch hue (R10): a muted plum, deliberately distinct from
/// the blocked `scRust` so a purged span never reads as blocked. Used by the
/// strip's purged hatch, its caption, and the legend's purged swatch (U6).
/// Matches the palette's SCTile3 violet (`#7A4A8A`); authored as a literal
/// (the `SCGradient` pattern) rather than a new asset role.
extension Color {
    static let scPlum = Color(.sRGB, red: 0x7A / 255, green: 0x4A / 255, blue: 0x8A / 255)
}

struct DayStripView: View {
    let bounds: DayStripLayout.Bounds
    /// The "unsplit — still searchable" base layer — one per recording.
    let baseTracks: [DayStripBaseTrack]
    /// Agent/user task bands overlaid on the base tracks (SCR-214).
    let segments: [DayStripSegment]
    let blockedBands: [DayStripBlockedBand]
    /// Anchored search-match timestamps (epoch ms).
    let matchesMs: [Int]
    let playheadMs: Int?
    var onSeek: (Int) -> Void
    /// U5 — retroactively-purged spans (R10): the blocked hatch geometry in the
    /// distinct plum hue, with R6 rule-naming hover/VoiceOver copy.
    var purgedBands: [DayPurgedInterval] = []
    /// U5 — `unverifiable` intervals inside recordings: rendered neutrally (no
    /// band — R7) but hover/VoiceOver-targetable with the "can't verify" copy.
    var unverifiableBands: [(startMs: Int, endMs: Int)] = []
    /// U5 — the day's spans + end-status provenance feeding the gap-cause
    /// resolver (R4).
    var spanProvenance: [DayStripLayout.SpanProvenance] = []
    /// U5 — store gate: false while the vault is sealed → every empty stretch
    /// reads "can't verify", never "nothing on file".
    var storeMounted: Bool = true
    /// Coverage gate: false when a recording with an unreadable `recording.db`
    /// couldn't be placed on the day (`coverage_complete` on the `timeline.day`
    /// response) → "nothing on file" claims degrade to "can't verify".
    var coverageComplete: Bool = true
    /// U5 — load gate: false until the day query returns; while false, gap
    /// regions carry NO cause claim (neutral copy only). Defaults to false so
    /// a call site that never wires provenance can't over-claim.
    var provenanceReady: Bool = false
    /// The "now" used to bound "still recording" and silence future time
    /// (R11). Injectable for determinism; defaults to wall-clock at render.
    var nowMs: Int = Int(Date().timeIntervalSince1970 * 1000)
    /// SCR-214 U11 — the in-progress retroactive task selection (both endpoints
    /// marked), drawn as a dashed amber band so the user sees the span they're
    /// about to label. Nil when no selection is in flight (default) — additive,
    /// so existing call sites are unaffected.
    var pendingSelection: (startMs: Int, endMs: Int)? = nil
    /// SCR-214 U11 — a single marked endpoint awaiting its partner, drawn as a
    /// standalone tick.
    var pendingEndpointMs: Int? = nil
    /// U4 (AE3) — a task span to emphasize when the day page is opened from
    /// Tasks or Chat: a subtle outline + glow around the matching task band.
    /// Nil (default) when there is nothing to highlight, so existing call sites
    /// are unaffected. Resolved to the exact band via `resolveHighlightSpan`.
    var highlightedSpan: (startMs: Int, endMs: Int)? = nil

    /// Strip metrics per the design (444–461): 64pt band area, 16pt track.
    private let stripHeight: CGFloat = 64
    private let trackTop: CGFloat = 24
    private let trackHeight: CGFloat = 16

    private static let boundaryTimeFormatter: DateFormatter = {
        let f = DateFormatter()
        f.dateFormat = "HH:mm"
        return f
    }()

    static func boundaryTimeString(_ ms: Int) -> String {
        boundaryTimeFormatter.string(from: Date(timeIntervalSince1970: Double(ms) / 1000))
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            GeometryReader { geo in
                strip(width: geo.size.width)
            }
            .frame(height: stripHeight)
            hourLabels
            legend
        }
    }

    // MARK: - Strip

    private func strip(width: CGFloat) -> some View {
        ZStack(alignment: .topLeading) {
            canvas(width: width)
            accessibilityOverlays(width: width)
        }
        .contentShape(Rectangle())
        .gesture(
            DragGesture(minimumDistance: 0)
                .onEnded { value in
                    onSeek(DayStripLayout.ms(forX: value.location.x, bounds: bounds, width: width))
                }
        )
    }

    private func canvas(width: CGFloat) -> some View {
        Canvas { ctx, _ in
            let trackRect = CGRect(x: 0, y: trackTop, width: width, height: trackHeight)
            let track = Path(roundedRect: trackRect, cornerRadius: 4)
            ctx.fill(track, with: .color(.scCanvas))
            ctx.stroke(track, with: .color(.scBorderWarm), lineWidth: 1)

            // The "unsplit — still searchable" base layer: each recording's full
            // span, softly tinted so its uncovered stretches read as unsplit-but-
            // searchable footage under the overlaid task bands (SCR-214).
            for base in baseTracks {
                let rect = bandRect(startMs: base.startMs, endMs: base.endMs, width: width)
                let path = Path(roundedRect: rect, cornerRadius: 4)
                ctx.fill(path, with: .color(.scTealSoft.opacity(0.15)))
                ctx.stroke(path, with: .color(.scTealSoft.opacity(0.30)), lineWidth: 1)
            }

            // Task labels above the bands (design 451–453): placement comes
            // from the pure `taskLabelFrames` (greedy left-to-right, skip on
            // overlap, 44pt minimum hint), so it's unit-testable without a
            // render. A truncated label draws an ellipsized string instead of
            // hard-clipping mid-glyph; every band still exposes its full task
            // name via the accessibility overlay + hover tooltip.
            let sortedSegments = segments.sorted { $0.startMs < $1.startMs }
            let segmentRects = sortedSegments.map {
                bandRect(startMs: $0.startMs, endMs: $0.endMs, width: width)
            }
            let resolvedLabels = sortedSegments.map { resolveTaskLabel($0.name, ctx: ctx) }
            let labelFrames = DayStripLayout.taskLabelFrames(
                bands: segmentRects.map { (minX: $0.minX, width: $0.width) },
                measuredWidths: resolvedLabels.map { $0.measure(in: taskLabelMaxSize).width }
            )
            for (index, rect) in segmentRects.enumerated() {
                let path = Path(roundedRect: rect, cornerRadius: 4)
                ctx.fill(path, with: .color(.scTeal.opacity(0.25)))
                ctx.stroke(path, with: .color(.scTeal.opacity(0.40)), lineWidth: 1)
                guard let frame = labelFrames[index] else { continue }
                let label = frame.truncated
                    ? ellipsizedTaskLabel(sortedSegments[index].name, maxWidth: frame.width, ctx: ctx)
                    : resolvedLabels[index]
                let drawWidth = min(frame.width, label.measure(in: taskLabelMaxSize).width)
                ctx.draw(label, in: CGRect(x: frame.minX, y: 0, width: drawWidth, height: 14))
            }

            // U4 (AE3) — a landing highlight from Tasks/Chat: a subtle teal
            // outline + glow around the emphasized task band. The span is
            // resolved to the exact band via the pure `resolveHighlightSpan`
            // (exact match → containing band → raw span), so it lands on the
            // task even before the bands finish loading.
            if let highlight = highlightedSpan {
                let resolved = DayStripLayout.resolveHighlightSpan(
                    highlight: highlight,
                    segments: segments.map { (startMs: $0.startMs, endMs: $0.endMs) }
                )
                let outline = bandRect(startMs: resolved.startMs, endMs: resolved.endMs, width: width)
                    .insetBy(dx: -2, dy: -3)
                let path = Path(roundedRect: outline, cornerRadius: 5)
                ctx.stroke(path, with: .color(.scTeal.opacity(0.35)), lineWidth: 5)
                ctx.stroke(path, with: .color(.scTeal), lineWidth: 2)
            }

            // Footage start/stop edge times (U1/AE1): mark where footage begins
            // and ends on the honest full-day axis, so a small band reads as
            // real footage at a real time rather than "a few minutes".
            for label in DayStripLayout.footageBoundaryLabels(
                spans: baseTracks.map { (startMs: $0.startMs, endMs: $0.endMs) }
            ) {
                let x = DayStripLayout.x(forMs: label.ms, bounds: bounds, width: width)
                let text = Text(Self.boundaryTimeString(label.ms))
                    .font(SCTypography.mono(size: 9))
                    .foregroundColor(.scInk.opacity(0.55))
                let boxW: CGFloat = 34
                let minX = label.kind == .started ? x + 2 : x - boxW - 2
                ctx.draw(text, in: CGRect(x: max(0, min(minX, width - boxW)), y: 12, width: boxW, height: 11))
            }

            for band in blockedBands {
                hatch(ctx: ctx, rect: bandRect(startMs: band.startMs, endMs: band.endMs, width: width),
                      color: .scRust)
            }
            // U5 — purged spans: the same hatch geometry in the distinct plum
            // hue (R10), so purged is visually its own class, never blocked.
            for band in purgedBands {
                hatch(ctx: ctx, rect: bandRect(startMs: band.startMs, endMs: band.endMs, width: width),
                      color: .scPlum)
            }
            // One caption per cluster (pure `captionClusters` — single overlap
            // guard across all caption classes), not one per band: nearby
            // blocked bands share a caption instead of overprinting. U5 routes
            // purged bands in as `.purged`: a purged-only cluster captions
            // "removed" in plum (never "blocked" — AE10); a mixed cluster keeps
            // the "blocked" caption (the hard-stop claim dominates the shared
            // slot; the purged member is still visually plum + hover-explained).
            let captions = DayStripLayout.captionClusters(
                blockedBands.map {
                    (x: bandRect(startMs: $0.startMs, endMs: $0.endMs, width: width).minX, cls: .blocked)
                } + purgedBands.map {
                    (x: bandRect(startMs: $0.startMs, endMs: $0.endMs, width: width).minX, cls: .purged)
                }
            )
            for caption in captions {
                let text = caption.classes.contains(.blocked)
                    ? Text("blocked").font(SCTypography.mono(size: 9)).foregroundColor(.scRust)
                    : Text("removed").font(SCTypography.mono(size: 9)).foregroundColor(.scPlum)
                ctx.draw(
                    text,
                    in: CGRect(x: caption.x, y: trackTop + trackHeight + 8, width: 50, height: 12)
                )
            }

            let markerXs = DayStripLayout.clusterXs(
                matchesMs.map { DayStripLayout.x(forMs: $0, bounds: bounds, width: width) }
            )
            for x in markerXs {
                let rect = CGRect(x: x - 2, y: trackTop - 4, width: 4, height: trackHeight + 8)
                let glow = rect.insetBy(dx: -3, dy: -3)
                ctx.fill(Path(roundedRect: glow, cornerRadius: 5), with: .color(.scAmber.opacity(0.3)))
                ctx.fill(Path(roundedRect: rect, cornerRadius: 2), with: .color(.scAmber))
            }

            if let playheadMs {
                let x = DayStripLayout.x(forMs: playheadMs, bounds: bounds, width: width)
                let rect = CGRect(x: x - 1, y: trackTop - 10, width: 2, height: trackHeight + 20)
                ctx.fill(Path(rect), with: .color(.scInk))
            }

            // SCR-214 U11 — the in-progress retroactive task selection.
            if let selection = pendingSelection {
                let rect = bandRect(startMs: selection.startMs, endMs: selection.endMs, width: width)
                let path = Path(roundedRect: rect, cornerRadius: 4)
                ctx.fill(path, with: .color(.scAmber.opacity(0.18)))
                ctx.stroke(path, with: .color(.scAmber), style: StrokeStyle(lineWidth: 1.5, dash: [4, 3]))
            } else if let endpointMs = pendingEndpointMs {
                let x = DayStripLayout.x(forMs: endpointMs, bounds: bounds, width: width)
                let rect = CGRect(x: x - 1.5, y: trackTop - 6, width: 3, height: trackHeight + 12)
                ctx.fill(Path(roundedRect: rect, cornerRadius: 1.5), with: .color(.scAmber))
            }
        }
    }

    /// The task-label measurement box (one 14pt line, 220pt name cap).
    private let taskLabelMaxSize = CGSize(width: 220, height: 14)

    private func resolveTaskLabel(_ name: String, ctx: GraphicsContext) -> GraphicsContext.ResolvedText {
        ctx.resolve(Text(name).font(SCTypography.mono(size: 9.5)).foregroundColor(.scTeal))
    }

    /// Ellipsize a task name to `maxWidth`: binary-search the longest prefix
    /// whose text + "…" measures within the width, so a truncated label ends in
    /// a visible ellipsis instead of a hard clip. Runs inside the Canvas draw
    /// closure, so O(log n) resolve/measure calls, not one per character. Worst
    /// case falls back to a bare "…" (the 44pt hint floor makes that
    /// practically unreachable).
    private func ellipsizedTaskLabel(
        _ name: String, maxWidth: CGFloat, ctx: GraphicsContext
    ) -> GraphicsContext.ResolvedText {
        let characters = Array(name)
        var lo = 0
        var hi = characters.count
        var best: GraphicsContext.ResolvedText?
        while lo < hi {
            let mid = (lo + hi + 1) / 2
            let candidate = resolveTaskLabel(String(characters.prefix(mid)) + "…", ctx: ctx)
            if candidate.measure(in: taskLabelMaxSize).width <= maxWidth {
                best = candidate
                lo = mid
            } else {
                hi = mid - 1
            }
        }
        return best ?? resolveTaskLabel("…", ctx: ctx)
    }

    private func bandRect(startMs: Int, endMs: Int, width: CGFloat) -> CGRect {
        let x0 = DayStripLayout.x(forMs: startMs, bounds: bounds, width: width)
        let x1 = DayStripLayout.x(forMs: endMs, bounds: bounds, width: width)
        // ≥2pt so a very short span stays visible.
        return CGRect(x: x0, y: trackTop, width: max(x1 - x0, 2), height: trackHeight)
    }

    /// The design's hatch: 45° lines on a transparent band
    /// (`repeating-linear-gradient(45deg, #B4552B33 …)`, line 450). One
    /// geometry, two hues: blocked draws it in rust, purged in plum (R10) —
    /// the color is the class distinction, the pattern is shared.
    private func hatch(ctx: GraphicsContext, rect: CGRect, color: Color) {
        var clipped = ctx
        clipped.clip(to: Path(roundedRect: rect, cornerRadius: 4))
        var path = Path()
        var x = rect.minX - rect.height
        while x < rect.maxX {
            path.move(to: CGPoint(x: x, y: rect.minY))
            path.addLine(to: CGPoint(x: x + rect.height, y: rect.maxY))
            x += 6
        }
        clipped.stroke(path, with: .color(color.opacity(0.45)), lineWidth: 3)
    }

    /// Invisible positioned elements so VoiceOver announces every region type —
    /// unsplit base tracks, agent/user task bands, "nothing captured" gaps, the
    /// blocked caption, and the playhead time (the retired SearchDayTimeline's
    /// overlay pattern — Canvas content is opaque to accessibility). Focus order
    /// runs left→right in wall-clock time via `.accessibilitySortPriority`
    /// (higher = focused first), so a keyboard user sweeps the day in order
    /// across regions of every kind. A task band carries a `.help` tooltip with
    /// its full name so a truncated narrow band stays legible on hover.
    private func accessibilityOverlays(width: CGFloat) -> some View {
        ZStack(alignment: .topLeading) {
            ForEach(baseTracks) { base in
                let rect = bandRect(startMs: base.startMs, endMs: base.endMs, width: width)
                Color.clear
                    .frame(width: rect.width, height: rect.height)
                    .offset(x: rect.minX, y: rect.minY)
                    .accessibilityElement(children: .ignore)
                    .accessibilityLabel(DayStripAccessibility.baseTrackLabel(base))
                    .accessibilitySortPriority(Self.sortPriority(startMs: base.startMs))
            }
            ForEach(segments) { segment in
                let rect = bandRect(startMs: segment.startMs, endMs: segment.endMs, width: width)
                Color.clear
                    .frame(width: rect.width, height: rect.height)
                    .offset(x: rect.minX, y: rect.minY)
                    .accessibilityElement(children: .ignore)
                    .accessibilityLabel(DayStripAccessibility.segmentLabel(segment))
                    .accessibilityAddTraits(.isButton)
                    .accessibilitySortPriority(Self.sortPriority(startMs: segment.startMs))
                    .help(segment.name)
            }
            // U5 — the provenance layer: gaps (cause-resolved), blocked,
            // purged, and unverifiable regions in ONE hover/VoiceOver pass.
            // `hitFrames` expands slivers to the ~6pt minimum hit target and
            // orders wider regions first, so the smaller region renders later
            // — on top of base tracks, task bands, and its wider neighbours —
            // and wins the hover at boundary collisions (R13). The SAME cause
            // sentence feeds `.help` and the accessibility label (R12).
            let regions = provenanceRegions
            let frames = DayStripLayout.hitFrames(
                regions.map { region -> (minX: CGFloat, width: CGFloat) in
                    let rect = bandRect(startMs: region.startMs, endMs: region.endMs, width: width)
                    return (minX: rect.minX, width: rect.width)
                }
            )
            ForEach(frames, id: \.index) { frame in
                provenanceOverlay(region: regions[frame.index], frame: frame)
            }
            if let playheadMs {
                let x = DayStripLayout.x(forMs: playheadMs, bounds: bounds, width: width)
                Color.clear
                    .frame(width: 4, height: stripHeight)
                    .offset(x: x - 2, y: 0)
                    .accessibilityElement(children: .ignore)
                    .accessibilityLabel(DayStripAccessibility.playheadLabel(ms: playheadMs))
            }
            // U7 (R19) — the in-flight range selection, made perceivable by
            // VoiceOver: the dashed pending band + the lone endpoint tick are
            // Canvas-drawn (opaque to assistive tech), so mirror them as
            // positioned a11y elements carrying the endpoints/span in wall-clock
            // time. Exactly one is present at a time (a lone endpoint before the
            // span completes, the span after).
            if let selection = pendingSelection {
                let rect = bandRect(startMs: selection.startMs, endMs: selection.endMs, width: width)
                Color.clear
                    .frame(width: rect.width, height: rect.height)
                    .offset(x: rect.minX, y: rect.minY)
                    .accessibilityElement(children: .ignore)
                    .accessibilityLabel(DayStripAccessibility.pendingSelectionLabel(
                        startMs: selection.startMs, endMs: selection.endMs
                    ))
                    .accessibilitySortPriority(Self.sortPriority(startMs: selection.startMs))
            } else if let endpointMs = pendingEndpointMs {
                let x = DayStripLayout.x(forMs: endpointMs, bounds: bounds, width: width)
                Color.clear
                    .frame(width: 4, height: stripHeight)
                    .offset(x: x - 2, y: 0)
                    .accessibilityElement(children: .ignore)
                    .accessibilityLabel(DayStripAccessibility.pendingEndpointLabel(ms: endpointMs))
                    .accessibilitySortPriority(Self.sortPriority(startMs: endpointMs))
            }
        }
        .accessibilityHint("Click the strip to move playback")
    }

    /// "Nothing captured" gaps between recordings, derived from the base tracks.
    private var nothingCapturedGaps: [(startMs: Int, endMs: Int)] {
        DayStripLayout.gaps(
            bounds: bounds,
            spans: baseTracks.map { (startMs: $0.startMs, endMs: $0.endMs) }
        )
    }

    /// U5 — one hover/VoiceOver region on the provenance layer. `claim` is the
    /// single honest cause sentence, shared verbatim by `.help` and the
    /// accessibility label (R12); nil = no claim (the load gate), where
    /// VoiceOver falls back to the neutral unavailable copy.
    private struct ProvenanceRegion {
        let startMs: Int
        let endMs: Int
        let claim: String?
        let accessibilityFallback: String?

        var accessibilityLabel: String { claim ?? accessibilityFallback ?? "" }
    }

    /// Build the provenance layer: each gap (split at now so a claim never
    /// leaks into future time, R11) resolved to its one cause via the pure
    /// `gapCause`; plus blocked, purged, and unverifiable intervals. A future
    /// gap contributes nothing (future time claims NOTHING); an unloaded day
    /// contributes claim-free regions with neutral VoiceOver copy (load gate).
    private var provenanceRegions: [ProvenanceRegion] {
        var regions: [ProvenanceRegion] = []
        for gap in nothingCapturedGaps {
            for part in DayStripLayout.splitGapAtNow(startMs: gap.startMs, endMs: gap.endMs, nowMs: nowMs) {
                let cause = DayStripLayout.gapCause(
                    gapStartMs: part.startMs, gapEndMs: part.endMs,
                    spans: spanProvenance, storeMounted: storeMounted,
                    coverageComplete: coverageComplete,
                    provenanceReady: provenanceReady, nowMs: nowMs
                )
                if let claim = DayStripAccessibility.gapCauseLabel(
                    cause, startMs: part.startMs, endMs: part.endMs
                ) {
                    regions.append(ProvenanceRegion(
                        startMs: part.startMs, endMs: part.endMs,
                        claim: claim, accessibilityFallback: nil
                    ))
                } else if !provenanceReady {
                    regions.append(ProvenanceRegion(
                        startMs: part.startMs, endMs: part.endMs,
                        claim: nil,
                        accessibilityFallback: DayStripAccessibility.gapPendingLabel(
                            startMs: part.startMs, endMs: part.endMs
                        )
                    ))
                }
            }
        }
        for band in blockedBands {
            regions.append(ProvenanceRegion(
                startMs: band.startMs, endMs: band.endMs,
                claim: DayStripAccessibility.blockedLabel(band), accessibilityFallback: nil
            ))
        }
        for band in purgedBands {
            regions.append(ProvenanceRegion(
                startMs: band.startMs, endMs: band.endMs,
                claim: DayStripAccessibility.purgedLabel(band), accessibilityFallback: nil
            ))
        }
        for band in unverifiableBands {
            regions.append(ProvenanceRegion(
                startMs: band.startMs, endMs: band.endMs,
                claim: DayStripAccessibility.cantVerifyLabel(startMs: band.startMs, endMs: band.endMs),
                accessibilityFallback: nil
            ))
        }
        return regions
    }

    /// One provenance overlay element: an invisible frame carrying the cause
    /// as tooltip + accessibility label. `.help` attaches only when there is a
    /// claim — a claim-free region is VoiceOver-reachable (neutral copy) but
    /// asserts nothing on hover.
    @ViewBuilder
    private func provenanceOverlay(
        region: ProvenanceRegion, frame: DayStripLayout.HitFrame
    ) -> some View {
        let base = Color.clear
            .frame(width: frame.width, height: trackHeight)
            .offset(x: frame.minX, y: trackTop)
            .accessibilityElement(children: .ignore)
            .accessibilityLabel(region.accessibilityLabel)
            .accessibilitySortPriority(Self.sortPriority(startMs: region.startMs))
        if let claim = region.claim {
            base.help(claim)
        } else {
            base
        }
    }

    /// Later regions get a lower priority, so VoiceOver / keyboard focus walks
    /// the strip left→right in time. Only the *relative* order matters to
    /// `.accessibilitySortPriority`, so negating the start ms is enough.
    private static func sortPriority(startMs: Int) -> Double {
        -Double(startMs)
    }

    // MARK: - Labels + legend

    private var hourLabels: some View {
        GeometryReader { geo in
            ZStack(alignment: .topLeading) {
                ForEach(DayStripLayout.hourTicks(bounds: bounds), id: \.self) { ms in
                    let text = DayStripAccessibility.hourText(ms: ms)
                    // Centered on the tick from the measured width via the pure
                    // `tickLabelMinX` (the old fixed -14pt offset assumed one
                    // width and pushed the edge labels outside the strip).
                    Text(text)
                        .font(SCTypography.mono(size: 10))
                        .foregroundStyle(Color.scInkMuted)
                        .offset(x: DayStripLayout.tickLabelMinX(
                            tickX: DayStripLayout.x(forMs: ms, bounds: bounds, width: geo.size.width),
                            labelWidth: Self.hourLabelWidth(text),
                            stripWidth: geo.size.width
                        ))
                }
            }
        }
        .frame(height: 14)
    }

    /// Measure an hour label ("HH:mm") in the row's mono face — the width the
    /// pure `tickLabelMinX` centers/clamps with. Falls back to the monospaced
    /// system font if the bundled face is somehow unregistered, mirroring
    /// `Font.custom`'s own fallback.
    private static let hourLabelFont: NSFont =
        NSFont(name: SCFonts.IBMPlexMono.regular, size: 10)
            ?? NSFont.monospacedSystemFont(ofSize: 10, weight: .regular)

    private static func hourLabelWidth(_ text: String) -> CGFloat {
        (text as NSString).size(withAttributes: [.font: hourLabelFont]).width
    }

    private var legend: some View {
        // U6: one plain-language vocabulary (see `DayStripLegend`), including
        // the purged entry with its own plum swatch (R10). Driven off the pure
        // legend model so the copy and the render can't drift.
        HStack(spacing: 16) {
            ForEach(DayStripLegend.items) { item in
                legendItem(text: item.text) { swatch(for: item.swatch) }
            }
        }
        .font(SCTypography.mono(size: 10))
        .foregroundStyle(Color.scInkMuted)
        .padding(.top, 2)
    }

    @ViewBuilder
    private func swatch(for kind: DayStripLegend.Swatch) -> some View {
        switch kind {
        case .task:
            RoundedRectangle(cornerRadius: 2)
                .fill(Color.scTeal.opacity(0.25))
                .overlay(RoundedRectangle(cornerRadius: 2).strokeBorder(Color.scTeal.opacity(0.40), lineWidth: 1))
                .frame(width: 10, height: 8)
        case .unsplit:
            RoundedRectangle(cornerRadius: 2)
                .fill(Color.scTealSoft.opacity(0.15))
                .overlay(RoundedRectangle(cornerRadius: 2).strokeBorder(Color.scTealSoft.opacity(0.30), lineWidth: 1))
                .frame(width: 10, height: 8)
        case .nothingCaptured:
            RoundedRectangle(cornerRadius: 2)
                .fill(Color.scCanvas)
                .overlay(RoundedRectangle(cornerRadius: 2).strokeBorder(Color.scBorderWarm, lineWidth: 1))
                .frame(width: 10, height: 8)
        case .searchMatch:
            RoundedRectangle(cornerRadius: 2)
                .fill(Color.scAmber)
                .frame(width: 3, height: 10)
        case .blocked:
            RoundedRectangle(cornerRadius: 2)
                .fill(Color.scRust.opacity(0.25))
                .frame(width: 10, height: 8)
        case .purged:
            // R10: the purged swatch is the plum hue — the same distinction the
            // strip's hatch draws — never the blocked rust.
            RoundedRectangle(cornerRadius: 2)
                .fill(Color.scPlum.opacity(0.25))
                .frame(width: 10, height: 8)
        }
    }

    private func legendItem(text: String, @ViewBuilder swatch: () -> some View) -> some View {
        HStack(spacing: 6) {
            swatch()
            Text(text)
        }
    }
}

/// Label builders for the strip's accessibility overlays (the
/// SearchAccessibility pattern — pure, string-level testable).
enum DayStripAccessibility {
    /// An agent/user task band: its name + wall-clock range.
    static func segmentLabel(_ segment: DayStripSegment) -> String {
        "\(segment.name), task, \(hourMinuteText(ms: segment.startMs)) to \(hourMinuteText(ms: segment.endMs))"
    }

    /// A recording's unsplit base track — footage that exists and is searchable
    /// but hasn't been carved into a task (SCR-214). Names the recording so a
    /// keyboard user still knows which recording the span belongs to.
    static func baseTrackLabel(_ base: DayStripBaseTrack) -> String {
        "\(base.title), unsplit, still searchable, \(hourMinuteText(ms: base.startMs)) to \(hourMinuteText(ms: base.endMs))"
    }

    // U6: the legacy blanket `gapLabel` ("Nothing captured, …") is retired —
    // gaps speak exclusively through the U5 cause labels (`gapCauseLabel`),
    // so an empty stretch always carries its one honest cause (R8/R9).

    // MARK: - U5: gap-cause + purged copy (one string home — the SAME sentence
    // feeds `.help` (hover) and `.accessibilityLabel` (VoiceOver), so
    // provenance is never pointer-only, R12).

    /// Exactly one cause + time range per empty stretch (R4). "Nothing on
    /// file" is a data claim — never "no recording was running". Returns nil
    /// for `.none` (future time / not loaded), which must claim nothing.
    static func gapCauseLabel(_ cause: DayStripLayout.GapCause, startMs: Int, endMs: Int) -> String? {
        let range = "\(hourMinuteText(ms: startMs)) to \(hourMinuteText(ms: endMs))"
        switch cause {
        case .nothingOnFile:
            return "Nothing on file, \(range)"
        case .interrupted(let aroundMs):
            return "Recording was cut short around \(hourMinuteText(ms: aroundMs))"
        case .stillRecording:
            return "Still recording"
        case .cantVerify:
            return cantVerifyLabel(startMs: startMs, endMs: endMs)
        case .none:
            return nil
        }
    }

    /// An unprovable stretch — a sealed store, unknown provenance, or an
    /// `unverifiable` interval inside a recording (R7: unprovable → say so).
    static func cantVerifyLabel(startMs: Int, endMs: Int) -> String {
        "Can't verify what happened here, \(hourMinuteText(ms: startMs)) to \(hourMinuteText(ms: endMs))"
    }

    /// A retroactively-purged span (R6): name the rule when identity is on the
    /// wire — app name preferred, then root domain, then bundle id — and
    /// degrade to the generic privacy-rule sentence when identity-free.
    static func purgedLabel(_ interval: DayPurgedInterval) -> String {
        let range = "\(hourMinuteText(ms: interval.startMs)) to \(hourMinuteText(ms: interval.endMs))"
        if let identity = interval.appName ?? interval.rootDomain ?? interval.bundleId {
            return "Removed by your 'disable \(identity)' rule, \(range)"
        }
        return "Removed by a privacy rule, \(range)"
    }

    /// The load-gate gap label: while the day query hasn't returned (or
    /// failed), gaps carry NO cause claim — VoiceOver gets this neutral
    /// unavailable string instead.
    static func gapPendingLabel(startMs: Int, endMs: Int) -> String {
        "Day details unavailable, \(hourMinuteText(ms: startMs)) to \(hourMinuteText(ms: endMs))"
    }

    /// A provably-blocked band (U6 vocabulary): "blocked at capture" — the
    /// hard-stop claim without the redundant "nothing captured" pairing, and
    /// the same wording the legend and the recorded-summary pane use (R9).
    static func blockedLabel(_ band: DayStripBlockedBand) -> String {
        "Blocked at capture, \(hourMinuteText(ms: band.startMs)) to \(hourMinuteText(ms: band.endMs))"
    }

    static func playheadLabel(ms: Int) -> String {
        "Playhead at \(hourMinuteText(ms: ms))"
    }

    /// U7 (R19) — the in-flight range selection span, announced to VoiceOver so
    /// the gesture is perceivable without sight (the Canvas band is opaque to
    /// assistive tech).
    static func pendingSelectionLabel(startMs: Int, endMs: Int) -> String {
        "Range selection, \(hourMinuteText(ms: startMs)) to \(hourMinuteText(ms: endMs))"
    }

    /// U7 (R19) — a single placed endpoint awaiting its partner.
    static func pendingEndpointLabel(ms: Int) -> String {
        "Range start marked at \(hourMinuteText(ms: ms)), set the end"
    }

    static func hourText(ms: Int) -> String {
        Self.hourFormatter.string(from: Date(timeIntervalSince1970: Double(ms) / 1000))
    }

    static func hourMinuteText(ms: Int) -> String {
        Self.hourMinuteFormatter.string(from: Date(timeIntervalSince1970: Double(ms) / 1000))
    }

    private static let hourFormatter: DateFormatter = {
        let f = DateFormatter()
        f.dateFormat = "HH:mm"
        return f
    }()

    private static let hourMinuteFormatter: DateFormatter = {
        let f = DateFormatter()
        f.dateFormat = "HH:mm"
        return f
    }()
}
