import AppKit
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

    /// The height twin of the invariant above (SCR-301).
    ///
    /// This replaced a flat 320pt "day chrome" budget, which passed while saying
    /// nothing useful: 320pt covers the header and strip almost exactly, so the
    /// assertion held no matter how tall the narrative grew — and a long
    /// narrative is what pushed the header off the top of the page.
    func testWindowMinimumHeightCoversTheDayPagesHardMinimum() {
        XCTAssertLessThanOrEqual(
            ShellWindowLayout.minContentHeight,
            ShellWindowLayout.windowMinHeight,
            """
            The day page needs \(ShellWindowLayout.minContentHeight)pt \
            (\(ShellWindowLayout.dayHeaderMinHeight)pt header + \
            \(ShellWindowLayout.playbackPaneMinHeight)pt pane floor + \
            \(ShellWindowLayout.dayStripMinHeight)pt strip + \
            \(ShellWindowLayout.dayNarrativeMinHeight)pt narrative) but the \
            window's declared minimum is \(ShellWindowLayout.windowMinHeight)pt. \
            At the floor the page's VStack overflows and the header — the only \
            in-page way off the day — is drawn outside the window.
            """
        )
    }

    /// A page too short to hold the chrome must drop the narrative rather than
    /// hand back a negative frame — SwiftUI complains at runtime about those,
    /// and a negative cap would make the stack overflow again by exactly the
    /// amount it went below zero.
    func testNarrativeCapNeverGoesNegative() {
        XCTAssertEqual(ShellWindowLayout.dayNarrativeMaxHeight(inPageHeight: 0), 0)
        XCTAssertEqual(ShellWindowLayout.dayNarrativeMaxHeight(inPageHeight: 200), 0)
        XCTAssertGreaterThanOrEqual(
            ShellWindowLayout.dayNarrativeMaxHeight(
                inPageHeight: ShellWindowLayout.windowMinHeight
            ),
            ShellWindowLayout.dayNarrativeMinHeight,
            "a narrative should still be readable at the minimum window height"
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

    // MARK: - Measured day-page section heights (SCR-301)

    /// The detail column at the window's minimum width — the narrowest the day
    /// page ever lays out in, so the tallest its sections ever wrap to.
    private static let detailWidth: CGFloat =
        ShellWindowLayout.windowMinWidth - ShellWindowLayout.sidebarWidth

    /// The day whose header is tallest. The header's date label renders
    /// "EEEE, d MMMM" in a fixed en_US locale, and at the minimum detail width a
    /// long one wraps to a second line — 70pt against 63pt for a short date. A
    /// fixture built from `Date()` would therefore measure a different header
    /// depending on the day the suite runs, and pass on a short date while the
    /// budget was short for most of the year. 30 September 2026 is a Wednesday:
    /// the longest weekday with the longest month name.
    private static let tallestHeaderDate: Date = {
        var calendar = Calendar(identifier: .gregorian)
        calendar.locale = Locale(identifier: "en_US")
        return calendar.date(from: DateComponents(year: 2026, month: 9, day: 30))!
    }()

    private static func dayPage() -> DayTimelineView {
        DayTimelineView(date: tallestHeaderDate, onBack: {})
    }

    /// The height budgets are estimates of what AppKit draws, exactly like the
    /// two control widths above, and they carry the same consequence: budget the
    /// header or strip short and the narrative's cap hands out height the page
    /// does not have, the stack overflows, and the header goes off the top with
    /// no test failing.
    ///
    /// Measured at the **minimum** detail width, which is where these sections
    /// are tallest — the strip's legend wraps to a second row below roughly
    /// 1100pt and gains 13pt. And measured from the frame SwiftUI resolves, not
    /// from `NSView.fittingSize`: fitting size reports the wrapped-away 151pt
    /// even at a width where the page really lays the strip out at 164pt, which
    /// is precisely the 13pt of overflow this guard exists to catch.
    @MainActor
    func testMeasuredDayPageSectionHeightsAreWithinBudget() {
        let view = Self.dayPage()

        let headerHeight = Self.laidOutHeight(of: view.header, named: "header")
        XCTAssertLessThanOrEqual(
            headerHeight,
            ShellWindowLayout.dayHeaderMinHeight,
            """
            The day header lays out at \(headerHeight)pt against a budgeted \
            \(ShellWindowLayout.dayHeaderMinHeight)pt. Raise \
            dayHeaderMinHeight to the measured value and re-check \
            windowMinHeight.
            """
        )

        let stripHeight = Self.laidOutHeight(of: view.strip, named: "strip")
        XCTAssertLessThanOrEqual(
            stripHeight,
            ShellWindowLayout.dayStripMinHeight,
            """
            The day strip lays out at \(stripHeight)pt against a budgeted \
            \(ShellWindowLayout.dayStripMinHeight)pt — same consequence as the \
            header above.
            """
        )
    }

    /// The height a section resolves to at the narrowest detail column.
    @MainActor
    private static func laidOutHeight(of section: some View, named name: String) -> CGFloat {
        let sink = SectionFrames()
        let hosted = ViewHost.host(
            section
                .reportingFrame(as: name, to: sink)
                .coordinateSpace(name: SectionFrames.pageSpace)
                .frame(width: detailWidth),
            size: CGSize(width: detailWidth, height: 600)
        )
        return withExtendedLifetime(hosted) { sink.frames[name]?.height ?? .infinity }
    }

    /// A day with no narrative must not pay for one — no blank band on every
    /// mechanical day.
    ///
    /// Measured inside `DayPageLayout`, like every other height assertion below,
    /// because the cap only behaves as designed in a real stack. Hosted on its
    /// own, a `.frame(maxHeight:)` fills whatever its parent proposes and
    /// reports the full cap even for an empty section; inside the page the
    /// VStack sizes it against its siblings and it takes only what it needs.
    /// Measuring the section alone answers a question the layout never asks.
    @MainActor
    func testAbsentNarrativeTakesNoHeightInThePage() {
        let view = Self.dayPage()
        let frames = Self.hostDayPage(
            view,
            narrative: view.narrativeSection,
            pageHeight: ShellWindowLayout.windowDefaultHeight
        )

        XCTAssertNil(
            frames["narrative"],
            "a day with no narrative should show no narrative slot"
        )
    }

    /// The other half of the cap: a short narrative keeps its natural height
    /// rather than stretching to fill the space the page could spare. This is
    /// what `ViewThatFits` buys — a bare `ScrollView` would take the whole cap
    /// and render a one-line summary as a card ten lines tall.
    @MainActor
    func testShortNarrativeKeepsItsNaturalHeightInThePage() {
        let view = Self.dayPage()
        let pageHeight: CGFloat = 900
        let cap = ShellWindowLayout.dayNarrativeMaxHeight(inPageHeight: pageHeight)
        let frames = Self.hostDayPage(
            view,
            narrative: view.narrativeCard("A quiet morning of reading."),
            pageHeight: pageHeight
        )

        guard let narrative = frames["narrative"] else {
            return XCTFail("the short narrative was not drawn at all")
        }
        XCTAssertLessThan(
            narrative.height, cap,
            """
            A one-line narrative laid out at \(narrative.height)pt against a \
            \(cap)pt cap — it is stretching to fill the slot instead of taking \
            its natural height.
            """
        )
    }

    /// The reported failure, end to end (SCR-301).
    ///
    /// The day page at the minimum window height with a narrative far longer
    /// than the page can hold. Before the cap, the VStack overflowed
    /// symmetrically and the header's date picker was drawn 60pt **above** the
    /// top of the page — the whole header row gone, and with it every way to
    /// leave the day.
    ///
    /// Both page edges are checked, because the cap can fail at either one. The
    /// original layout pushed the header off the top (caught by the first
    /// assertion); drop the cap while keeping the top anchor and the overflow
    /// simply moves to the bottom and takes the strip with it instead — visible
    /// only to the second. The top anchor itself has no failing case here by
    /// design: while the cap holds there is nothing to overflow, so it is
    /// insurance against a term exceeding its budget at runtime, not a term in
    /// the arithmetic.
    ///
    /// The third page height is not a window size — it is the window floor minus
    /// the ~147pt first-run privacy banner, which `MainWindow.shellContent`
    /// stacks above the two-column row. That page is shorter than the day
    /// chrome, so the cap goes to zero and the narrative has to be dropped
    /// outright: a `.frame(maxHeight: 0)` would still draw the card's 44pt of
    /// chrome centred on the empty slot, straight over the header.
    @MainActor
    func testTheWholeDayPageStaysOnThePageWhenTheNarrativeIsLong() {
        let view = Self.dayPage()
        let narrative = String(
            repeating: "Worked through the migration backlog and reviewed the pipeline changes. ",
            count: 40
        )

        for pageHeight in [
            ShellWindowLayout.windowMinHeight,
            ShellWindowLayout.windowDefaultHeight,
            ShellWindowLayout.windowMinHeight - Self.firstRunBannerHeight,
        ] {
            let frames = Self.hostDayPage(
                view,
                narrative: view.narrativeCard(narrative),
                pageHeight: pageHeight
            )

            guard let header = frames["header"], let strip = frames["strip"] else {
                return XCTFail("the page reported no section frames at \(pageHeight)pt")
            }

            XCTAssertGreaterThanOrEqual(
                header.minY, 0,
                """
                At a \(pageHeight)pt page the header is drawn at y=\
                \(header.minY) — above the top of the page, so the Back button \
                and date navigator are not on screen at all. The day page's \
                vertical terms no longer fit; check the narrative cap and the \
                measured section heights.
                """
            )
            // On a page that cannot seat a readable narrative the section is
            // dropped, so there is no strip position to assert — only that the
            // header survived, which the assertion above already covers.
            let cap = ShellWindowLayout.dayNarrativeMaxHeight(inPageHeight: pageHeight)
            guard cap >= ShellWindowLayout.dayNarrativeMinHeight else {
                XCTAssertNil(
                    frames["narrative"],
                    """
                    At a \(pageHeight)pt page there is only \(cap)pt for the \
                    narrative, but it was still drawn at \
                    \(String(describing: frames["narrative"])). A capped card \
                    does not shrink past its own chrome — it renders over the \
                    header. Drop the section instead.
                    """
                )
                continue
            }

            XCTAssertLessThanOrEqual(
                strip.maxY, pageHeight,
                """
                At a \(pageHeight)pt page the strip ends at y=\(strip.maxY) — \
                below the page. The stack is overflowing; the narrative cap is \
                the term that keeps it inside.
                """
            )
        }
    }

    /// `FirstRunPrivacyBanner`'s height including the gutter `MainWindow` gives
    /// it — the amount the day page loses while the banner is up.
    @MainActor
    private static var firstRunBannerHeight: CGFloat {
        laidOutHeight(
            of: FirstRunPrivacyBanner(onReview: {}, onDismiss: {})
                .padding(.horizontal, 16)
                .padding(.top, 12),
            named: "banner"
        )
    }

    /// Lay the day page out at `pageHeight` and return where its sections landed.
    ///
    /// The playback pane is stood in for rather than built: its whole
    /// contribution to the page's height is its floor plus its willingness to
    /// grow, and an `AVPlayer` in a unit test is neither needed nor welcome.
    /// Header, narrative and strip are the real views.
    @MainActor
    private static func hostDayPage(
        _ view: DayTimelineView,
        narrative: some View,
        pageHeight: CGFloat
    ) -> [String: CGRect] {
        let sections = SectionFrames()
        let page = DayPageLayout {
            view.header.reportingFrame(as: "header", to: sections)
        } narrative: {
            narrative.reportingFrame(as: "narrative", to: sections)
        } pane: {
            Color.clear.frame(
                maxWidth: .infinity,
                minHeight: ShellWindowLayout.playbackPaneMinHeight,
                maxHeight: .infinity
            )
        } strip: {
            view.strip.reportingFrame(as: "strip", to: sections)
        }
        .coordinateSpace(name: SectionFrames.pageSpace)
        .frame(width: detailWidth, height: pageHeight)

        let hosted = ViewHost.host(
            page, size: CGSize(width: detailWidth, height: pageHeight)
        )
        return withExtendedLifetime(hosted) { sections.frames }
    }
}

// MARK: - Section geometry

/// Collects sections' frames in the day page's own coordinate space.
///
/// The page is almost entirely SwiftUI, which draws no discrete `NSView`s to
/// measure, so the geometry is read where SwiftUI resolves it rather than
/// inferred from whatever AppKit controls happen to be in the tree — those
/// report their own intrinsic size, not the slot they were given.
private final class SectionFrames {
    static let pageSpace = "day-page"
    var frames: [String: CGRect] = [:]
}

private struct SectionFrameKey: PreferenceKey {
    static var defaultValue: [String: CGRect] = [:]
    static func reduce(value: inout [String: CGRect], nextValue: () -> [String: CGRect]) {
        value.merge(nextValue()) { _, latest in latest }
    }
}

private extension View {
    func reportingFrame(as name: String, to sink: SectionFrames) -> some View {
        background(
            GeometryReader { geo in
                Color.clear.preference(
                    key: SectionFrameKey.self,
                    value: [name: geo.frame(in: .named(SectionFrames.pageSpace))]
                )
            }
        )
        .onPreferenceChange(SectionFrameKey.self) { sink.frames.merge($0) { _, latest in latest } }
    }
}
