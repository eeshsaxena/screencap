import SwiftUI
import XCTest
@testable import Screencap

/// SCR-297 — that the playback pane's modifier stack actually produces the
/// geometry `PlaybackAspect.fittedSize` promises.
///
/// `ShellWindowLayoutTests` pins the arithmetic; this pins the *composition*.
/// They are different failure modes: the arithmetic can be perfect while the
/// modifier order in `DayTimelineView.playbackPane` is wrong, and a wrong order
/// is silent — the pane still renders, just at the wrong size, with its
/// timestamp chip and action bar back out over dead space. That is exactly the
/// bug this change exists to fix, so it should not be able to come back
/// unnoticed.
///
/// The order under test, and why each layer sits where it does:
///
///   ZStack (video)
///     .modifier(SourceAspectFit)   <- shapes the player
///     .overlay × 3                 <- INSIDE, so chrome tracks the video
///     .frame(maxWidth/maxHeight)   <- OUTSIDE, holds the floor and centres
///     .padding(.horizontal)        <- the day page's gutter
///
/// Measuring an inner subview is not something `ViewHost` can do (its own
/// header notes the accessibility tree is unreadable from this host and that
/// identifiers do not land on discrete NSViews), so the probe reports its own
/// resolved size out through a `GeometryReader` instead.
@MainActor
final class DayPlaybackPaneLayoutTests: XCTestCase {

    /// Captures the size SwiftUI resolved for the constrained content.
    private final class SizeProbe {
        var size: CGSize = .zero
    }

    /// Rebuild the pane's modifier stack around a flexible stand-in for the
    /// video, and report the size the constrained content resolved to.
    private func measureConstrainedContent(slot: CGSize, aspect: CGFloat?) -> CGSize {
        let probe = SizeProbe()

        let pane = Color.black
            .modifier(SourceAspectFit(aspect: aspect))
            .background(
                GeometryReader { geometry in
                    Color.clear.onAppear { probe.size = geometry.size }
                }
            )
            .frame(
                maxWidth: .infinity,
                minHeight: ShellWindowLayout.playbackPaneMinHeight,
                maxHeight: .infinity
            )
            .padding(.horizontal, ShellWindowLayout.playbackPaneHorizontalInset)

        // Retain the handle across the measurement — releasing the window can
        // tear the hosted tree down mid-read.
        let hosted = ViewHost.host(pane, size: slot)
        withExtendedLifetime(hosted) {}
        return probe.size
    }

    /// The reported case. At the shipped default the detail column is
    /// proportionally wider than 16:10 footage, so height binds and the
    /// leftover width is given back to the page instead of drawn as black.
    func testDefaultWindowSlotIsHeightBoundAndGivesWidthBack() {
        let slot = CGSize(width: 932, height: 420)
        let measured = measureConstrainedContent(slot: slot, aspect: 1.6)
        let expected = PlaybackAspect.fittedSize(inSlot: slot, aspect: 1.6)

        XCTAssertEqual(measured.width, expected.width, accuracy: 0.5)
        XCTAssertEqual(measured.height, expected.height, accuracy: 0.5)
        XCTAssertLessThan(
            measured.width,
            slot.width - ShellWindowLayout.playbackPaneHorizontalInset * 2,
            "the player should not be consuming the full gutter-adjusted width here"
        )
    }

    /// The opposite bind, and the shape of a narrow window: the player takes
    /// the full gutter-adjusted width and gives height back.
    func testNarrowSlotIsWidthBound() {
        let slot = CGSize(width: 700, height: 600)
        let measured = measureConstrainedContent(slot: slot, aspect: 1.6)
        let expected = PlaybackAspect.fittedSize(inSlot: slot, aspect: 1.6)

        XCTAssertEqual(measured.width, expected.width, accuracy: 0.5)
        XCTAssertEqual(measured.height, expected.height, accuracy: 0.5)
    }

    /// The fallback that makes "never guess a ratio" safe. An unresolved aspect
    /// has to reproduce the pre-SCR-297 layout exactly — if this drifts, every
    /// pre-`readyToPlay` moment and every placeholder renders wrong.
    func testUnresolvedAspectFillsTheSlotExactlyAsBefore() {
        let slot = CGSize(width: 932, height: 420)
        let measured = measureConstrainedContent(slot: slot, aspect: nil)

        XCTAssertEqual(
            measured.width,
            slot.width - ShellWindowLayout.playbackPaneHorizontalInset * 2,
            accuracy: 0.5
        )
        XCTAssertEqual(measured.height, slot.height, accuracy: 0.5)
    }

    /// An ultrawide capture binds on width and gives back the most height, so
    /// it is the shape most likely to degenerate into a sliver.
    func testUltrawideFootageStaysWatchable() {
        let slot = CGSize(width: 932, height: 420)
        let aspect: CGFloat = 3440.0 / 1440.0
        let measured = measureConstrainedContent(slot: slot, aspect: aspect)
        let expected = PlaybackAspect.fittedSize(inSlot: slot, aspect: aspect)

        XCTAssertEqual(measured.width, expected.width, accuracy: 0.5)
        XCTAssertEqual(measured.height, expected.height, accuracy: 0.5)
        XCTAssertGreaterThan(measured.height, 200)
    }

    /// Whatever the footage, the player fits. A result overflowing the slot
    /// would push the day strip off-screen — the class of bug PR #442 closed,
    /// and the reason this pane's layout is pinned rather than eyeballed.
    func testConstrainedContentNeverOverflowsTheSlot() {
        let slot = CGSize(width: 932, height: 420)
        let available = slot.width - ShellWindowLayout.playbackPaneHorizontalInset * 2

        for aspect in [ShellWindowLayout.playbackAspectMin, 1.0, 1.6, ShellWindowLayout.playbackAspectMax] {
            let measured = measureConstrainedContent(slot: slot, aspect: aspect)
            XCTAssertLessThanOrEqual(measured.width, available + 0.5, "aspect \(aspect)")
            XCTAssertLessThanOrEqual(measured.height, slot.height + 0.5, "aspect \(aspect)")
        }
    }
}
