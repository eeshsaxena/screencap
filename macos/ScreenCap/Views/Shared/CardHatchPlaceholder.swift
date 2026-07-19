import SwiftUI

/// The design's diagonal hatch fill (`repeating-linear-gradient(45deg, …)`) —
/// the card thumbnail placeholder when a recording has no readable frame. Drawn
/// with a `Canvas` so it scales crisply at any card width.
///
/// Shared across the Days, Chat, Timeline, and Recall surfaces (relocated out of
/// the retired Library views).
struct CardHatchPlaceholder: View {
    /// 1px lines every 11px, matching the design's `#EDE6D6 10px, 11px` stops.
    private let spacing: CGFloat = 11

    var body: some View {
        Canvas { ctx, size in
            var path = Path()
            // 45° lines sweeping across the rect: each runs from the top edge
            // down-right to the bottom edge, offset by `spacing`.
            var x = -size.height
            while x < size.width {
                path.move(to: CGPoint(x: x, y: 0))
                path.addLine(to: CGPoint(x: x + size.height, y: size.height))
                x += spacing
            }
            ctx.stroke(path, with: .color(.scFillSubtle), lineWidth: 1)
        }
        .background(Color.scCanvas)
        .accessibilityHidden(true)
    }
}
