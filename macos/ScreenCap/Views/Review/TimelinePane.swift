import Foundation
import SwiftUI

/// Plan U6: action timeline. A Canvas-drawn horizontal strip showing each
/// event as a vertical tick mark colored by category, with an overlay
/// cursor that follows the video's `currentTime`. Drag gestures translate
/// X coordinates to recording-relative seconds and call `onScrub` so the
/// parent can seek the player.
///
/// Visual encoding (single-row colored markers) is the defensible default
/// the plan calls out under Open Questions; iterate from real-recording
/// feedback rather than over-designing up front.
/// A recording-relative time interval (risky-moment band), end clamped to the
/// recording duration so an open-ended interval renders to the timeline's edge.
struct TimelineInterval: Equatable {
    let start: Double
    let end: Double
}

struct TimelinePane: View {
    let events: [TimelineEvent]
    let durationSeconds: Double
    let currentTime: Double
    /// Advisory risky-moment intervals (R13) — drawn as translucent bands, a
    /// distinct encoding from the per-category event ticks. Advisory only;
    /// never gates Upload.
    var riskyIntervals: [TimelineInterval] = []
    /// Per-moment redaction markers (R8) — drawn as ticks along the top edge in
    /// a distinct color, separate from the event ticks below.
    var redactionMarkers: [Double] = []
    let onScrub: (Double) -> Void

    var body: some View {
        GeometryReader { geo in
            ZStack(alignment: .topLeading) {
                Canvas { context, size in
                    drawBackground(context: context, size: size)
                    drawRiskyBands(context: context, size: size)
                    drawMarkers(context: context, size: size)
                    drawRedactionMarkers(context: context, size: size)
                    drawCursor(context: context, size: size)
                }
                .gesture(
                    DragGesture(minimumDistance: 0)
                        .onChanged { value in
                            let seconds = TimelinePaneScrub.scrubSeconds(
                                forX: value.location.x,
                                width: geo.size.width,
                                durationSeconds: durationSeconds
                            )
                            onScrub(seconds)
                        }
                )
            }
        }
        .frame(minHeight: 56)
    }

    private func drawBackground(context: GraphicsContext, size: CGSize) {
        context.fill(Path(CGRect(origin: .zero, size: size)), with: .color(.gray.opacity(0.08)))
    }

    private func drawMarkers(context: GraphicsContext, size: CGSize) {
        guard durationSeconds > 0 else { return }
        let baseline = size.height / 2
        let halfTickHeight = max(8, size.height / 3)
        // Batch markers into one Path per category so we issue at most one
        // `context.stroke` per category bucket instead of one per event.
        // With 10k events × the ~10Hz periodic-time-observer redraw cadence
        // the per-event allocation pattern would burn ~100k Path() allocs
        // and strokes per second; this drops it to 5.
        var pathsByCategory: [TimelineEvent.Category: Path] = [:]
        for event in events {
            let ratio = min(1, max(0, event.relativeSeconds / durationSeconds))
            let x = CGFloat(ratio) * size.width
            pathsByCategory[event.category, default: Path()].move(to: CGPoint(x: x, y: baseline - halfTickHeight))
            pathsByCategory[event.category]?.addLine(to: CGPoint(x: x, y: baseline + halfTickHeight))
        }
        for (category, path) in pathsByCategory {
            context.stroke(path, with: .color(color(for: category)), lineWidth: 1.5)
        }
    }

    /// R13: translucent amber bands spanning each risky interval — a distinct
    /// encoding from the event ticks (a span, not a point) so the operator can
    /// tell "the scrubber acted over this stretch" apart from discrete events.
    private func drawRiskyBands(context: GraphicsContext, size: CGSize) {
        guard durationSeconds > 0 else { return }
        for interval in riskyIntervals {
            let x0 = TimelinePaneScrub.cursorX(
                forSeconds: interval.start, width: size.width, durationSeconds: durationSeconds)
            let x1 = TimelinePaneScrub.cursorX(
                forSeconds: interval.end, width: size.width, durationSeconds: durationSeconds)
            let rect = CGRect(x: x0, y: 0, width: max(2, x1 - x0), height: size.height)
            context.fill(Path(rect), with: .color(.orange.opacity(0.18)))
        }
    }

    /// R8: short ticks along the TOP edge marking moments where content was
    /// redacted — distinct color and position from the mid-height event ticks.
    private func drawRedactionMarkers(context: GraphicsContext, size: CGSize) {
        guard durationSeconds > 0, !redactionMarkers.isEmpty else { return }
        var path = Path()
        let topInset: CGFloat = 0
        let tickHeight: CGFloat = max(6, size.height / 4)
        for t in redactionMarkers {
            let x = TimelinePaneScrub.cursorX(
                forSeconds: t, width: size.width, durationSeconds: durationSeconds)
            path.move(to: CGPoint(x: x, y: topInset))
            path.addLine(to: CGPoint(x: x, y: topInset + tickHeight))
        }
        context.stroke(path, with: .color(.pink), lineWidth: 2)
    }

    private func drawCursor(context: GraphicsContext, size: CGSize) {
        guard durationSeconds > 0 else { return }
        let ratio = min(1, max(0, currentTime / durationSeconds))
        let x = CGFloat(ratio) * size.width
        var path = Path()
        path.move(to: CGPoint(x: x, y: 0))
        path.addLine(to: CGPoint(x: x, y: size.height))
        context.stroke(path, with: .color(.accentColor), lineWidth: 2)
    }

    private func color(for category: TimelineEvent.Category) -> Color {
        switch category {
        case .mouse:  return .blue
        case .key:    return .green
        case .window: return .purple
        case .screen: return .orange
        case .other:  return .gray
        }
    }
}

/// Pure logic helpers extracted so unit tests can assert the coordinate-to-
/// timestamp mapping without needing a SwiftUI render.
enum TimelinePaneScrub {
    /// Clamps `x` into `[0, width]` and maps it to a recording-relative
    /// timestamp inside `[0, durationSeconds]`. A non-positive `width` or
    /// `durationSeconds` returns 0 (no division by zero, no NaN).
    static func scrubSeconds(forX x: CGFloat, width: CGFloat, durationSeconds: Double) -> Double {
        guard width > 0, durationSeconds > 0 else { return 0 }
        let clamped = min(width, max(0, x))
        let ratio = Double(clamped / width)
        return min(durationSeconds, max(0, ratio * durationSeconds))
    }

    /// Inverse of `scrubSeconds(forX:...)` — useful for asserting the
    /// cursor's drawn X coordinate in tests without invoking Canvas.
    static func cursorX(forSeconds seconds: Double, width: CGFloat, durationSeconds: Double) -> CGFloat {
        guard width > 0, durationSeconds > 0 else { return 0 }
        let ratio = min(1, max(0, seconds / durationSeconds))
        return CGFloat(ratio) * width
    }
}

/// Converts the envelope's redaction evidence (absolute-epoch timestamps, the
/// same space the events use) into the recording-relative markers/intervals the
/// timeline draws. Extracted so the conversion + clamping is unit-tested.
enum RedactionTimeline {
    /// Per-moment redaction marker times, relative to recording start, sorted
    /// and clamped to >= 0.
    static func relativeMarkers(_ redaction: ReviewRedaction?, startedAt: Double) -> [Double] {
        (redaction?.markers ?? [])
            .map { max(0, $0.t - startedAt) }
            .sorted()
    }

    /// Risky-moment intervals relative to recording start. An open-ended
    /// interval (`end == nil`) clamps to `duration` so it renders to the
    /// timeline's edge; zero/unknown duration yields no intervals (nothing to
    /// place them against).
    static func riskyIntervals(
        _ redaction: ReviewRedaction?, startedAt: Double, duration: Double
    ) -> [TimelineInterval] {
        guard duration > 0 else { return [] }
        return (redaction?.blockedIntervals ?? []).map { iv in
            let start = max(0, iv.start - startedAt)
            let end = iv.end.map { max(start, $0 - startedAt) } ?? duration
            return TimelineInterval(start: min(start, duration), end: min(end, duration))
        }
    }
}
