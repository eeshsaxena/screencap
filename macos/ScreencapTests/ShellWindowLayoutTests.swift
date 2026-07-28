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

    // MARK: - Source-aspect resolution (SCR-297)

    /// The clamp exists to reject nonsense, not to police unusual hardware. If
    /// it ever tightened past a real display shape the pane would silently fall
    /// back to filling the box for that user and the pillarboxing would return
    /// for them alone — a per-hardware bug nothing else would catch.
    func testAspectClampAdmitsEveryRealisticDisplayShape() {
        XCTAssertLessThan(ShellWindowLayout.playbackAspectMin, ShellWindowLayout.playbackAspectMax)

        for (label, ratio) in [
            ("4:3", 4.0 / 3.0),
            ("16:10", 16.0 / 10.0),
            ("16:9", 16.0 / 9.0),
            ("21:9 ultrawide", 3440.0 / 1440.0),
            ("portrait-rotated", 1080.0 / 1920.0),
        ] as [(String, CGFloat)] {
            XCTAssertNotNil(
                PlaybackAspect.resolve(reportedSize: CGSize(width: ratio, height: 1)),
                "\(label) (\(ratio)) must resolve — it is a real display shape"
            )
        }
    }

    func testAspectResolvesRealCaptureSizes() {
        XCTAssertEqual(PlaybackAspect.resolve(reportedSize: CGSize(width: 1920, height: 1200)), 1.6)
        XCTAssertEqual(
            PlaybackAspect.resolve(reportedSize: CGSize(width: 3440, height: 1440)) ?? 0,
            2.3889, accuracy: 0.001
        )
        XCTAssertEqual(
            PlaybackAspect.resolve(reportedSize: CGSize(width: 1080, height: 1920)) ?? 0,
            0.5625, accuracy: 0.0001
        )
    }

    /// `.zero` is the ordinary pre-`readyToPlay` state, not corruption — it has
    /// to resolve to nil so the pane renders as it does today rather than
    /// collapsing.
    func testAspectIsUnresolvedForDegenerateSizes() {
        XCTAssertNil(PlaybackAspect.resolve(reportedSize: .zero))
        XCTAssertNil(PlaybackAspect.resolve(reportedSize: CGSize(width: 1920, height: 0)))
        XCTAssertNil(PlaybackAspect.resolve(reportedSize: CGSize(width: 0, height: 1080)))
        XCTAssertNil(PlaybackAspect.resolve(reportedSize: CGSize(width: -1920, height: 1080)))
        XCTAssertNil(PlaybackAspect.resolve(reportedSize: CGSize(width: CGFloat.infinity, height: 1080)))
        XCTAssertNil(PlaybackAspect.resolve(reportedSize: CGSize(width: 1920, height: CGFloat.nan)))
    }

    func testAspectIsUnresolvedOutsideTheClamp() {
        XCTAssertNil(PlaybackAspect.resolve(reportedSize: CGSize(width: 8000, height: 100)))
        XCTAssertNil(PlaybackAspect.resolve(reportedSize: CGSize(width: 100, height: 8000)))
    }

    // MARK: - Fitted player size (SCR-297)

    /// The default window: the slot is proportionally wider than 16:10 footage,
    /// so height binds and the leftover width becomes page background. This is
    /// the case the ticket was filed about.
    func testFittedSizeIsHeightBoundInASlotWiderThanTheFootage() {
        let slot = CGSize(width: 932, height: 420)
        let fitted = PlaybackAspect.fittedSize(inSlot: slot, aspect: 1.6)

        XCTAssertEqual(fitted.height, slot.height)
        XCTAssertLessThan(fitted.width, slot.width - ShellWindowLayout.playbackPaneHorizontalInset * 2)
        XCTAssertEqual(fitted.width / fitted.height, 1.6, accuracy: 0.0001)
    }

    /// The opposite bind: the player takes the full gutter-adjusted width and
    /// gives height back.
    func testFittedSizeIsWidthBoundInASlotNarrowerThanTheFootage() {
        let slot = CGSize(width: 700, height: 600)
        let fitted = PlaybackAspect.fittedSize(inSlot: slot, aspect: 1.6)

        XCTAssertEqual(
            fitted.width,
            slot.width - ShellWindowLayout.playbackPaneHorizontalInset * 2
        )
        XCTAssertLessThan(fitted.height, slot.height)
        XCTAssertEqual(fitted.width / fitted.height, 1.6, accuracy: 0.0001)
    }

    /// The fallback that makes "never guess a ratio" safe: unresolved must
    /// reproduce the pre-SCR-297 layout exactly, not approximately.
    func testUnresolvedAspectReturnsTheFullInsetAdjustedSlot() {
        let slot = CGSize(width: 932, height: 420)
        let fitted = PlaybackAspect.fittedSize(inSlot: slot, aspect: nil)

        XCTAssertEqual(
            fitted,
            CGSize(
                width: slot.width - ShellWindowLayout.playbackPaneHorizontalInset * 2,
                height: slot.height
            )
        )
    }

    /// An ultrawide capture is the shape most likely to degenerate, since it
    /// binds on width and gives back the most height.
    func testUltrawideFootageKeepsAWatchableHeightAtTheDefaultWindow() {
        let slot = CGSize(width: 932, height: 420)
        let fitted = PlaybackAspect.fittedSize(inSlot: slot, aspect: 3440.0 / 1440.0)

        XCTAssertEqual(
            fitted.width,
            slot.width - ShellWindowLayout.playbackPaneHorizontalInset * 2
        )
        XCTAssertGreaterThan(fitted.height, 200, "an ultrawide day should not render as a sliver")
    }

    /// The invariant that matters most: whatever the footage, the player fits.
    /// A result exceeding the slot would push the strip off-screen and re-open
    /// the overflow class of bug PR #442 closed.
    func testFittedSizeNeverExceedsTheSlotForAnyAdmittedRatio() {
        let slots = [
            CGSize(width: 752, height: 320),   // minimum window
            CGSize(width: 932, height: 420),   // default window
            CGSize(width: 2312, height: 1000), // maximized on a large display
        ]
        let ratios = stride(
            from: ShellWindowLayout.playbackAspectMin,
            through: ShellWindowLayout.playbackAspectMax,
            by: 0.1
        )

        for slot in slots {
            let available = slot.width - ShellWindowLayout.playbackPaneHorizontalInset * 2
            for ratio in ratios {
                let fitted = PlaybackAspect.fittedSize(inSlot: slot, aspect: ratio)
                XCTAssertLessThanOrEqual(fitted.width, available, "ratio \(ratio) in slot \(slot)")
                XCTAssertLessThanOrEqual(fitted.height, slot.height, "ratio \(ratio) in slot \(slot)")
                XCTAssertGreaterThan(fitted.width, 0, "ratio \(ratio) in slot \(slot)")
                XCTAssertGreaterThan(fitted.height, 0, "ratio \(ratio) in slot \(slot)")
            }
        }
    }

    /// A slot too narrow to hold its own gutter must clamp to zero width rather
    /// than going negative — a negative frame is a SwiftUI runtime complaint,
    /// and the arithmetic should not be able to produce one at any window size.
    func testFittedSizeDegradesSafelyWhenTheSlotCannotHoldTheGutter() {
        let fitted = PlaybackAspect.fittedSize(
            inSlot: CGSize(width: 10, height: 100), aspect: 1.6
        )
        XCTAssertEqual(fitted.width, 0)
        XCTAssertGreaterThanOrEqual(fitted.height, 0)
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
