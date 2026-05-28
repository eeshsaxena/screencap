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
struct TimelinePane: View {
    let events: [TimelineEvent]
    let durationSeconds: Double
    let currentTime: Double
    let onScrub: (Double) -> Void

    var body: some View {
        GeometryReader { geo in
            ZStack(alignment: .topLeading) {
                Canvas { context, size in
                    drawBackground(context: context, size: size)
                    drawMarkers(context: context, size: size)
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
        let bg = Path(CGRect(origin: .zero, size: size))
        context.fill(bg, with: .color(.gray.opacity(0.08)))
    }

    private func drawMarkers(context: GraphicsContext, size: CGSize) {
        guard durationSeconds > 0 else { return }
        let baseline = size.height / 2
        let halfTickHeight = max(8, size.height / 3)
        for event in events {
            let ratio = min(1, max(0, event.relativeSeconds / durationSeconds))
            let x = CGFloat(ratio) * size.width
            var path = Path()
            path.move(to: CGPoint(x: x, y: baseline - halfTickHeight))
            path.addLine(to: CGPoint(x: x, y: baseline + halfTickHeight))
            context.stroke(path, with: .color(color(for: event.category)), lineWidth: 1.5)
        }
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
