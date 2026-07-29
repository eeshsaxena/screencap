import SwiftUI

/// The Day page's vertical contract (SCR-301).
///
/// The page stacks four sections and three of them cannot give height back (see
/// `ShellWindowLayout`'s day-page vertical terms). The narrative is the one that
/// grows with its content, so this layout is where it stops being unbounded: it
/// is capped at whatever the page's own height leaves once the header, the
/// playback pane's floor and the strip have taken theirs.
///
/// The cap needs the page's height, which is why the stack lives inside a
/// `GeometryReader` rather than filling its parent directly. That is also what
/// lets the stack be pinned to the **top**: SwiftUI centres a stack it cannot
/// fit, so before this the overflow was split between both ends and the header
/// went off the top. Anchored to the top, any overflow that a future section
/// might still cause costs the strip's legend instead — visible, recoverable,
/// and never the page's only way back.
///
/// Extracted from `DayTimelineView.body` so the arithmetic has one home and can
/// be hosted and measured by `ShellWindowLayoutTests`.
struct DayPageLayout<Header: View, Narrative: View, Pane: View, Strip: View>: View {
    @ViewBuilder let header: () -> Header
    @ViewBuilder let narrative: () -> Narrative
    @ViewBuilder let pane: () -> Pane
    @ViewBuilder let strip: () -> Strip

    var body: some View {
        GeometryReader { geo in
            let narrativeCap = ShellWindowLayout.dayNarrativeMaxHeight(
                inPageHeight: geo.size.height
            )
            VStack(spacing: 0) {
                header()
                // Below a readable slot the narrative is dropped, not squeezed.
                // A `.frame(maxHeight:)` neither clips the card nor shrinks it
                // past its own 44pt of chrome, so a near-zero cap would draw the
                // card's border centred on an empty slot — over the header. That
                // page height is reachable today: the first-run privacy banner
                // takes ~147pt off the page while it is up.
                if narrativeCap >= ShellWindowLayout.dayNarrativeMinHeight {
                    narrative().frame(maxHeight: narrativeCap, alignment: .top)
                }
                pane()
                strip()
            }
            .frame(width: geo.size.width, height: geo.size.height, alignment: .top)
        }
    }
}
