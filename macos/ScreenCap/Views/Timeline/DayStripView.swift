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

            // Greedy left-to-right label placement: a label is skipped when it
            // would overlap the previously drawn one (adjacent narrow tasks would
            // otherwise paint on top of each other). Every band still exposes its
            // task name via the accessibility overlay + hover tooltip. The label
            // is clipped to the band width so a very narrow band truncates (the
            // full name stays reachable via the overlay's `.help`).
            var lastLabelEndX = -CGFloat.greatestFiniteMagnitude
            for segment in segments.sorted(by: { $0.startMs < $1.startMs }) {
                let rect = bandRect(startMs: segment.startMs, endMs: segment.endMs, width: width)
                let path = Path(roundedRect: rect, cornerRadius: 4)
                ctx.fill(path, with: .color(.scTeal.opacity(0.25)))
                ctx.stroke(path, with: .color(.scTeal.opacity(0.40)), lineWidth: 1)
                // Task label above the band (design 451–453).
                let label = ctx.resolve(
                    Text(segment.name).font(SCTypography.mono(size: 9.5)).foregroundColor(.scTeal)
                )
                let measured = label.measure(in: CGSize(width: 220, height: 14))
                if rect.minX >= lastLabelEndX + 8 {
                    // Truncate the drawn label to the band width (min 44pt so a
                    // one-word hint survives); the overlay tooltip carries the
                    // full name.
                    let drawWidth = min(measured.width, max(rect.width, 44))
                    ctx.draw(label, in: CGRect(x: rect.minX, y: 0, width: drawWidth, height: 14))
                    lastLabelEndX = rect.minX + drawWidth
                }
            }

            for band in blockedBands {
                let rect = bandRect(startMs: band.startMs, endMs: band.endMs, width: width)
                hatch(ctx: ctx, rect: rect)
                ctx.draw(
                    Text("blocked").font(SCTypography.mono(size: 9)).foregroundColor(.scRust),
                    in: CGRect(x: rect.minX, y: trackTop + trackHeight + 8, width: max(rect.width, 50), height: 12)
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
                    Text(DayStripAccessibility.hourText(ms: ms))
                        .font(SCTypography.mono(size: 10))
                        .foregroundStyle(Color.scInkMuted)
                        .offset(x: DayStripLayout.x(forMs: ms, bounds: bounds, width: geo.size.width) - 14)
                }
            }
        }
        .frame(height: 14)
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
