import SwiftUI
import XCTest
@testable import Screencap

/// The main window's width contract (`ShellWindowLayout`).
///
/// The bug these guard against: the window declared an 880pt minimum while the
/// Day page needed ~1000pt of hard minimums (248pt sidebar + ~750pt of header
/// chrome). A `SwiftUI` `HStack` that cannot fit its hard minimums overflows
/// **symmetrically** rather than clipping, so at the declared floor the
/// sidebar's leading edge and the playback pane's trailing action bar were both
/// drawn outside the window — reported from the field as content "hanging out
/// both sides".
///
/// The failure mode is silent (no assertion fires, nothing logs, only a visual
/// defect at one window size on one route), so the arithmetic is pinned here.
final class ShellWindowLayoutTests: XCTestCase {

    /// The founding invariant. A window minimum below the shell's real
    /// requirement does not degrade gracefully — it overflows both edges.
    func testWindowMinimumWidthCoversTheShellsHardMinimum() {
        XCTAssertGreaterThanOrEqual(
            ShellWindowLayout.windowMinWidth,
            ShellWindowLayout.minContentWidth,
            """
            The window's declared minimum (\(ShellWindowLayout.windowMinWidth)pt) is \
            narrower than the Day page actually lays out in \
            (\(ShellWindowLayout.minContentWidth)pt = \
            \(ShellWindowLayout.sidebarWidth)pt sidebar + \
            \(ShellWindowLayout.dayHeaderMinWidth)pt header). At the floor the \
            shell HStack overflows symmetrically and content is drawn outside \
            both window edges. Either raise windowMinWidth or soften a hard \
            minimum in the header.
            """
        )
    }

    /// The default size must be a real default, not the floor. Opening at the
    /// minimum is what put the reporter's window at the overflow boundary in
    /// the first place, and it is also what squeezed the playback pane into a
    /// ~3:1 letterbox strip.
    func testWindowOpensAboveItsMinimum() {
        XCTAssertGreaterThan(ShellWindowLayout.windowDefaultWidth, ShellWindowLayout.windowMinWidth)
        XCTAssertGreaterThan(ShellWindowLayout.windowDefaultHeight, ShellWindowLayout.windowMinHeight)
    }

    /// The playback pane's floor has to actually fit inside the window's floor,
    /// otherwise the guard above is satisfied while the pane still forces
    /// vertical overflow. Header + narrative + strip are budgeted generously.
    func testPlaybackPaneFloorFitsTheMinimumWindowHeight() {
        let dayChromeHeight: CGFloat = 320
        XCTAssertLessThanOrEqual(
            ShellWindowLayout.playbackPaneMinHeight + dayChromeHeight,
            ShellWindowLayout.windowMinHeight,
            "the playback pane's floor plus the day page chrome must fit at the minimum window height"
        )
    }

    /// The player's inset exists to align its edges with the header content
    /// above it. If the two drift apart the page grows a third alignment and the
    /// inset stops reading as deliberate — so the relationship is pinned, not
    /// left as two constants that happen to match today.
    func testPlaybackPaneInsetMatchesTheDayPageGutter() {
        XCTAssertEqual(
            ShellWindowLayout.playbackPaneHorizontalInset,
            ShellWindowLayout.dayHeaderHorizontalPadding,
            "the player's edges should line up with the header content above it"
        )
    }

    /// The search field may compress, but never past its declared floor, and
    /// its floor must not exceed its preferred width.
    func testSearchFieldFloorIsNotWiderThanItsPreferredWidth() {
        XCTAssertLessThanOrEqual(
            ShellWindowLayout.daySearchFieldMinWidth,
            ShellWindowLayout.daySearchFieldMaxWidth
        )
    }

    // MARK: - Measured control widths

    /// Everything in `minContentWidth` is a value we declare — except two, which
    /// are *estimates* of what AppKit renders: the `.field` DatePicker's
    /// intrinsic width and the "← Back" label's natural width. Neither
    /// compresses, so if either draws wider than budgeted the header's real
    /// minimum exceeds `minContentWidth`, the window minimum is too low again,
    /// and the overflow returns with no test failing.
    ///
    /// So measure them rather than trusting the estimate. This also catches a
    /// future macOS or font change widening the control out from under us.
    @MainActor
    func testEstimatedControlWidthsAreWithinBudget() {
        let picker = DatePicker(
            "", selection: .constant(Date()), displayedComponents: [.date]
        )
        .datePickerStyle(.field)
        .labelsHidden()
        let pickerWidth = ViewHost.host(
            picker, size: CGSize(width: 400, height: 60)
        ).root.fittingSize.width

        XCTAssertLessThanOrEqual(
            pickerWidth,
            ShellWindowLayout.dayDatePickerMinWidth,
            """
            The date picker renders \(pickerWidth)pt but the width contract \
            budgets \(ShellWindowLayout.dayDatePickerMinWidth)pt. The header's \
            real minimum is therefore wider than minContentWidth \
            (\(ShellWindowLayout.minContentWidth)pt) and the shell will overflow \
            at windowMinWidth again. Raise dayDatePickerMinWidth to the measured \
            value and re-derive windowMinWidth.
            """
        )

        let back = Text("← Back").font(SCTypography.sans(size: 13))
        let backWidth = ViewHost.host(
            back, size: CGSize(width: 400, height: 60)
        ).root.fittingSize.width

        XCTAssertLessThanOrEqual(
            backWidth,
            ShellWindowLayout.dayHeaderBackButtonWidth,
            """
            The Back label renders \(backWidth)pt against a budgeted \
            \(ShellWindowLayout.dayHeaderBackButtonWidth)pt — same consequence \
            as the date picker above.
            """
        )
    }
}
