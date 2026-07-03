import SwiftUI

// U9 — the horizontal day strip (design 444–467): labeled recording segment
// bands over a neutral base track, hatched provably-blocked bands, amber
// search-match markers, a black playhead, hour labels, and the legend. The
// geometry lives in the pure `DayStripLayout` (adapting SearchTimelineLayout)
// so time→x mapping and the dynamic axis bounds are unit-testable without a
// render (DayStripLayoutTests).

/// A recording span placed on the strip (from `/v0/timeline.day`), labeled with
/// the recording *title* — agent task labels arrive with SCR-214.
struct DayStripSegment: Equatable, Identifiable {
    let recording: String
    let title: String
    let startMs: Int
    let endMs: Int
    var id: String { recording }
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
    /// divide-by-zero (mirrors SearchTimelineLayout.placeMarkers).
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

    /// Collapse marker xs by single-linkage chaining (the SearchTimelineLayout
    /// clustering contract, same 14pt default threshold): walking left-to-right,
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
}

struct DayStripView: View {
    let bounds: DayStripLayout.Bounds
    let segments: [DayStripSegment]
    let blockedBands: [DayStripBlockedBand]
    /// Anchored search-match timestamps (epoch ms).
    let matchesMs: [Int]
    let playheadMs: Int?
    var onSeek: (Int) -> Void

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

            for segment in segments {
                let rect = bandRect(startMs: segment.startMs, endMs: segment.endMs, width: width)
                let path = Path(roundedRect: rect, cornerRadius: 4)
                ctx.fill(path, with: .color(.scTeal.opacity(0.25)))
                ctx.stroke(path, with: .color(.scTeal.opacity(0.33)), lineWidth: 1)
                // Segment label above the band (design 451–453) — recording
                // title until SCR-214's task labels.
                ctx.draw(
                    Text(segment.title).font(SCTypography.mono(size: 9.5)).foregroundColor(.scTeal),
                    in: CGRect(x: rect.minX, y: 0, width: max(rect.width, 120), height: 14)
                )
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

    /// Invisible positioned elements so VoiceOver announces segment titles, the
    /// blocked caption, and the playhead time (reusing the SearchDayTimeline
    /// overlay pattern — Canvas content is opaque to accessibility).
    private func accessibilityOverlays(width: CGFloat) -> some View {
        ZStack(alignment: .topLeading) {
            ForEach(segments) { segment in
                let rect = bandRect(startMs: segment.startMs, endMs: segment.endMs, width: width)
                Color.clear
                    .frame(width: rect.width, height: rect.height)
                    .offset(x: rect.minX, y: rect.minY)
                    .accessibilityElement(children: .ignore)
                    .accessibilityLabel(DayStripAccessibility.segmentLabel(segment))
                    .accessibilityAddTraits(.isButton)
            }
            ForEach(Array(blockedBands.enumerated()), id: \.offset) { _, band in
                let rect = bandRect(startMs: band.startMs, endMs: band.endMs, width: width)
                Color.clear
                    .frame(width: rect.width, height: rect.height)
                    .offset(x: rect.minX, y: rect.minY)
                    .accessibilityElement(children: .ignore)
                    .accessibilityLabel(DayStripAccessibility.blockedLabel(band))
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
        HStack(spacing: 16) {
            // Honesty substitutions on the design's legend copy (462–467):
            // "agent-labeled task" → "recording" until SCR-214's task labels,
            // and the base track is "nothing captured" — the design's
            // "unsplit — still searchable" describes footage, but our gaps
            // hold none (R7).
            legendItem(text: "recording") {
                RoundedRectangle(cornerRadius: 2)
                    .fill(Color.scTeal.opacity(0.25))
                    .overlay(RoundedRectangle(cornerRadius: 2).strokeBorder(Color.scTeal.opacity(0.33), lineWidth: 1))
                    .frame(width: 10, height: 8)
            }
            legendItem(text: "nothing captured") {
                RoundedRectangle(cornerRadius: 2)
                    .fill(Color.scCanvas)
                    .overlay(RoundedRectangle(cornerRadius: 2).strokeBorder(Color.scBorderWarm, lineWidth: 1))
                    .frame(width: 10, height: 8)
            }
            legendItem(text: "search match") {
                RoundedRectangle(cornerRadius: 2)
                    .fill(Color.scAmber)
                    .frame(width: 3, height: 10)
            }
            legendItem(text: "blocked — nothing captured") {
                RoundedRectangle(cornerRadius: 2)
                    .fill(Color.scRust.opacity(0.25))
                    .frame(width: 10, height: 8)
            }
        }
        .font(SCTypography.mono(size: 10))
        .foregroundStyle(Color.scInkMuted)
        .padding(.top, 2)
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
    static func segmentLabel(_ segment: DayStripSegment) -> String {
        "\(segment.title), \(hourMinuteText(ms: segment.startMs)) to \(hourMinuteText(ms: segment.endMs))"
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
