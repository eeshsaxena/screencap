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

/// Pure axis geometry for the day strip.
enum DayStripLayout {

    struct Bounds: Equatable {
        let startMs: Int
        let endMs: Int
        var spanMs: Int { endMs - startMs }
    }

    static let hourMs = 3_600_000
    static let minSpanMs = 8 * 3_600_000

    /// Dynamic axis bounds (plan assumption): the union of the day's recording
    /// spans rounded *outward* to the hour, minimum 8h, clamped into the day.
    /// A short union extends forward to 8h (pulling back from the day's end
    /// when needed); a spanless day defaults to the 08:00–16:00 working window.
    static func axisBounds(dayStartMs: Int, spans: [(startMs: Int, endMs: Int)]) -> Bounds {
        let dayEndMs = dayStartMs + 24 * hourMs
        guard let lo = spans.map(\.startMs).min(),
              let hi = spans.map(\.endMs).max()
        else {
            return Bounds(startMs: dayStartMs + 8 * hourMs, endMs: dayStartMs + 16 * hourMs)
        }
        func floorHour(_ ms: Int) -> Int {
            dayStartMs + ((ms - dayStartMs) / hourMs) * hourMs
        }
        func ceilHour(_ ms: Int) -> Int {
            let offset = ms - dayStartMs
            let rounded = (offset + hourMs - 1) / hourMs * hourMs
            return dayStartMs + rounded
        }
        var start = max(dayStartMs, floorHour(max(lo, dayStartMs)))
        var end = min(dayEndMs, ceilHour(min(hi, dayEndMs)))
        if end - start < minSpanMs {
            end = start + minSpanMs
            if end > dayEndMs {
                end = dayEndMs
                start = max(dayStartMs, end - minSpanMs)
            }
        }
        return Bounds(startMs: start, endMs: end)
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
        let sorted = xs.sorted()
        guard thresholdPx > 0, !sorted.isEmpty else { return sorted }
        var clusters: [CGFloat] = []
        var bucket: [CGFloat] = [sorted[0]]
        for x in sorted.dropFirst() {
            if x - bucket[bucket.count - 1] <= thresholdPx {
                bucket.append(x)
            } else {
                clusters.append(bucket.reduce(0, +) / CGFloat(bucket.count))
                bucket = [x]
            }
        }
        clusters.append(bucket.reduce(0, +) / CGFloat(bucket.count))
        return clusters
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

    /// A caption band's class. Only `blocked` renders today; `purged`
    /// (retroactively purged app spans) arrives in a later unit — the tag is
    /// modeled now so the mixed-class overlap guard can't drift when it does.
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
}

/// The strip legend's honest copy + swatch kinds. SCR-214 retires the pre-task
/// "honesty substitutions": `agent/user task` and `unsplit — still searchable`
/// now have real referents (ambient capture + on-device segmentation exist), so
/// the copy states them directly. Pure data so the entry set is unit-testable
/// without a render (the `DayStripAccessibility` pattern).
enum DayStripLegend {
    enum Swatch: Equatable {
        case task
        case unsplit
        case nothingCaptured
        case searchMatch
        case blocked
    }

    struct Item: Equatable, Identifiable {
        let text: String
        let swatch: Swatch
        var id: String { text }
    }

    static let items: [Item] = [
        Item(text: "agent/user task", swatch: .task),
        Item(text: "unsplit — still searchable", swatch: .unsplit),
        Item(text: "nothing captured", swatch: .nothingCaptured),
        Item(text: "search match", swatch: .searchMatch),
        Item(text: "blocked — nothing captured", swatch: .blocked),
    ]
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
    /// SCR-214 U11 — the in-progress retroactive task selection (both endpoints
    /// marked), drawn as a dashed amber band so the user sees the span they're
    /// about to label. Nil when no selection is in flight (default) — additive,
    /// so existing call sites are unaffected.
    var pendingSelection: (startMs: Int, endMs: Int)? = nil
    /// SCR-214 U11 — a single marked endpoint awaiting its partner, drawn as a
    /// standalone tick.
    var pendingEndpointMs: Int? = nil

    /// Strip metrics per the design (444–461): 64pt band area, 16pt track.
    private let stripHeight: CGFloat = 64
    private let trackTop: CGFloat = 24
    private let trackHeight: CGFloat = 16

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

            for band in blockedBands {
                hatch(ctx: ctx, rect: bandRect(startMs: band.startMs, endMs: band.endMs, width: width))
            }
            // One caption per cluster (pure `captionClusters` — single overlap
            // guard across all caption classes), not one per band: nearby
            // blocked bands share a caption instead of overprinting. Only
            // `.blocked` bands feed it today; purged joins in a later unit.
            let captions = DayStripLayout.captionClusters(
                blockedBands.map {
                    (x: bandRect(startMs: $0.startMs, endMs: $0.endMs, width: width).minX, cls: .blocked)
                }
            )
            for caption in captions {
                ctx.draw(
                    Text("blocked").font(SCTypography.mono(size: 9)).foregroundColor(.scRust),
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

    /// Ellipsize a task name to `maxWidth`: drop trailing characters until the
    /// name + "…" measures within the width, so a truncated label ends in a
    /// visible ellipsis instead of a hard clip. Worst case falls back to a bare
    /// "…" (the 44pt hint floor makes that practically unreachable).
    private func ellipsizedTaskLabel(
        _ name: String, maxWidth: CGFloat, ctx: GraphicsContext
    ) -> GraphicsContext.ResolvedText {
        var characters = Array(name)
        while !characters.isEmpty {
            let candidate = resolveTaskLabel(String(characters) + "…", ctx: ctx)
            if candidate.measure(in: taskLabelMaxSize).width <= maxWidth {
                return candidate
            }
            characters.removeLast()
        }
        return resolveTaskLabel("…", ctx: ctx)
    }

    private func bandRect(startMs: Int, endMs: Int, width: CGFloat) -> CGRect {
        let x0 = DayStripLayout.x(forMs: startMs, bounds: bounds, width: width)
        let x1 = DayStripLayout.x(forMs: endMs, bounds: bounds, width: width)
        // ≥2pt so a very short span stays visible.
        return CGRect(x: x0, y: trackTop, width: max(x1 - x0, 2), height: trackHeight)
    }

    /// The design's blocked hatch: 45° rust lines on a transparent band
    /// (`repeating-linear-gradient(45deg, #B4552B33 …)`, line 450).
    private func hatch(ctx: GraphicsContext, rect: CGRect) {
        var clipped = ctx
        clipped.clip(to: Path(roundedRect: rect, cornerRadius: 4))
        var path = Path()
        var x = rect.minX - rect.height
        while x < rect.maxX {
            path.move(to: CGPoint(x: x, y: rect.minY))
            path.addLine(to: CGPoint(x: x + rect.height, y: rect.maxY))
            x += 6
        }
        clipped.stroke(path, with: .color(.scRust.opacity(0.45)), lineWidth: 3)
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
            ForEach(nothingCapturedGaps, id: \.startMs) { gap in
                let rect = bandRect(startMs: gap.startMs, endMs: gap.endMs, width: width)
                Color.clear
                    .frame(width: rect.width, height: rect.height)
                    .offset(x: rect.minX, y: rect.minY)
                    .accessibilityElement(children: .ignore)
                    .accessibilityLabel(DayStripAccessibility.gapLabel(startMs: gap.startMs, endMs: gap.endMs))
                    .accessibilitySortPriority(Self.sortPriority(startMs: gap.startMs))
            }
            ForEach(Array(blockedBands.enumerated()), id: \.offset) { _, band in
                let rect = bandRect(startMs: band.startMs, endMs: band.endMs, width: width)
                Color.clear
                    .frame(width: rect.width, height: rect.height)
                    .offset(x: rect.minX, y: rect.minY)
                    .accessibilityElement(children: .ignore)
                    .accessibilityLabel(DayStripAccessibility.blockedLabel(band))
                    .accessibilitySortPriority(Self.sortPriority(startMs: band.startMs))
            }
            if let playheadMs {
                let x = DayStripLayout.x(forMs: playheadMs, bounds: bounds, width: width)
                Color.clear
                    .frame(width: 4, height: stripHeight)
                    .offset(x: x - 2, y: 0)
                    .accessibilityElement(children: .ignore)
                    .accessibilityLabel(DayStripAccessibility.playheadLabel(ms: playheadMs))
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
    private static func hourLabelWidth(_ text: String) -> CGFloat {
        let font = NSFont(name: SCFonts.IBMPlexMono.regular, size: 10)
            ?? NSFont.monospacedSystemFont(ofSize: 10, weight: .regular)
        return (text as NSString).size(withAttributes: [.font: font]).width
    }

    private var legend: some View {
        // SCR-214: the pre-task "honesty substitutions" are retired — the task
        // band and "unsplit — still searchable" swatches now have referents
        // (see `DayStripLegend`). Driven off the pure legend model so the copy
        // and the render can't drift.
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

    /// A between-recording gap: nothing was captured here (R7 — no footage).
    static func gapLabel(startMs: Int, endMs: Int) -> String {
        "Nothing captured, \(hourMinuteText(ms: startMs)) to \(hourMinuteText(ms: endMs))"
    }

    static func blockedLabel(_ band: DayStripBlockedBand) -> String {
        "Blocked, nothing captured, \(hourMinuteText(ms: band.startMs)) to \(hourMinuteText(ms: band.endMs))"
    }

    static func playheadLabel(ms: Int) -> String {
        "Playhead at \(hourMinuteText(ms: ms))"
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
