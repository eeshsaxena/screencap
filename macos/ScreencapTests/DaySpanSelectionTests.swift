import XCTest
@testable import Screencap

/// U7 (KTD-7, R9/R19) — the pure state machine behind the shared "Select range"
/// mode (`RangeSelectionModel`): mode entry/exit, endpoint marking, arrow-key
/// nudge, gap-spanning without a silent reset, and the programmatic
/// preselect-into-menu path. Rendering + wiring are verified by build-and-run;
/// these pin the logic. (The two-endpoint `DaySpanSelection`/`DaySpanSnap`
/// primitives are covered by ManualTaskCreationTests; this file exercises the
/// mode/menu layer on top of them.)
final class DaySpanSelectionTests: XCTestCase {

    // MARK: - Mode entry / exit

    func testStartsIdle() {
        let model = RangeSelectionModel()
        XCTAssertEqual(model.phase, .idle)
        XCTAssertFalse(model.isActive)
        XCTAssertNil(model.range)
        XCTAssertFalse(model.menuAnchored)
    }

    func testEnterSelectModeFromIdle() {
        var model = RangeSelectionModel()
        model.enterSelectMode()
        XCTAssertEqual(model.phase, .selecting)
        XCTAssertTrue(model.isActive)
        XCTAssertNil(model.range, "no endpoints marked yet")
    }

    func testEnterSelectModeIsIdleOnly() {
        var model = RangeSelectionModel()
        model.enterSelectMode()
        model.mark(1_000)
        model.mark(3_000)  // now .chosen with a range
        model.enterSelectMode()  // no-op — must not clobber an in-flight selection
        XCTAssertEqual(model.phase, .chosen)
        XCTAssertEqual(model.range?.startMs, 1_000)
        XCTAssertEqual(model.range?.endMs, 3_000)
    }

    // MARK: - Marking endpoints

    func testMarkRequiresActiveMode() {
        var model = RangeSelectionModel()
        model.mark(1_000)  // ignored while idle
        XCTAssertEqual(model.phase, .idle)
        XCTAssertNil(model.pendingEndpointMs)
    }

    func testFirstMarkLeavesLoneEndpoint() {
        var model = RangeSelectionModel()
        model.enterSelectMode()
        model.mark(2_000)
        XCTAssertEqual(model.phase, .selecting)
        XCTAssertEqual(model.pendingEndpointMs, 2_000)
        XCTAssertNil(model.range)
    }

    func testSecondEndpointCompletesRangeOrderedRegardlessOfMarkOrder() {
        var model = RangeSelectionModel()
        model.enterSelectMode()
        model.mark(3_000)
        model.mark(1_000)  // marked out of order
        XCTAssertEqual(model.phase, .chosen)
        XCTAssertEqual(model.range?.startMs, 1_000)
        XCTAssertEqual(model.range?.endMs, 3_000)
        XCTAssertTrue(model.menuAnchored)
        XCTAssertNil(model.pendingEndpointMs, "a complete range has no lone endpoint")
    }

    func testThirdMarkRestartsFromFreshEndpoint() {
        var model = RangeSelectionModel()
        model.enterSelectMode()
        model.mark(1_000)
        model.mark(2_000)
        model.mark(9_000)  // restart
        XCTAssertEqual(model.phase, .selecting)
        XCTAssertEqual(model.pendingEndpointMs, 9_000)
        XCTAssertNil(model.range)
    }

    // MARK: - Menu

    func testPresentMenuFromChosen() {
        var model = RangeSelectionModel()
        model.enterSelectMode()
        model.mark(1_000)
        model.mark(3_000)
        model.presentMenu()
        XCTAssertEqual(model.phase, .menu)
        XCTAssertTrue(model.menuAnchored)
    }

    func testPresentMenuNoopWithoutCompleteRange() {
        var model = RangeSelectionModel()
        model.enterSelectMode()
        model.mark(1_000)  // lone endpoint only
        model.presentMenu()
        XCTAssertEqual(model.phase, .selecting, "no menu without a complete range")
        XCTAssertFalse(model.menuAnchored)
    }

    // MARK: - Cancel / Esc never silently discards

    func testCancelFromSelectingReturnsIdleAndClears() {
        var model = RangeSelectionModel()
        model.enterSelectMode()
        model.mark(1_000)
        model.cancel()
        XCTAssertEqual(model.phase, .idle)
        XCTAssertNil(model.range)
        XCTAssertNil(model.pendingEndpointMs)
    }

    func testCancelFromMenuReturnsIdleAndClears() {
        var model = RangeSelectionModel()
        model.enterSelectMode()
        model.mark(1_000)
        model.mark(3_000)
        model.presentMenu()
        model.cancel()  // the visible Cancel / Esc — an EXPLICIT discard (R19)
        XCTAssertEqual(model.phase, .idle)
        XCTAssertNil(model.range)
    }

    // MARK: - Gap-spanning: NEVER silent-reset (R19)

    /// The old mark-a-task path silently reset the selection when the span's
    /// midpoint landed in a "nothing captured" gap. The generalized range mode
    /// keeps the span — the model has no gap knowledge, so a gap-spanning
    /// selection completes and survives; the caller shows why "save as task" is
    /// unavailable rather than dropping the range.
    func testGapSpanningSelectionIsKeptNotReset() {
        var model = RangeSelectionModel()
        model.enterSelectMode()
        // Two footage islands with a gap between 1_000 and 5_000.
        let tracks = [
            DayStripBaseTrack(recording: "morning", title: "morning", startMs: 0, endMs: 1_000),
            DayStripBaseTrack(recording: "afternoon", title: "afternoon", startMs: 5_000, endMs: 9_000),
        ]
        model.mark(500)     // inside the morning island
        model.mark(8_000)   // inside the afternoon island — midpoint (4_250) is in the gap
        XCTAssertEqual(model.range?.startMs, 500)
        XCTAssertEqual(model.range?.endMs, 8_000)
        XCTAssertTrue(model.menuAnchored, "a gap-spanning range still anchors the menu")
        // The reason "save as task" is unavailable — but the range is NOT dropped.
        XCTAssertNil(DaySpanSnap.recording(forRangeMidpoint: 500, 8_000, baseTracks: tracks))
    }

    // MARK: - Nudge (arrow-key precision)

    func testNudgeStepMatchesSnapTolerance() {
        // The nudge step is one snap tolerance (60 s), the boundary-scale step.
        XCTAssertEqual(RangeSelectionModel.nudgeStepMs, 60_000)
    }

    func testNudgeEndMovesTheLaterEndpointAndKeepsMenu() {
        var model = RangeSelectionModel()
        model.enterSelectMode()
        model.mark(100_000)
        model.mark(300_000)
        model.presentMenu()
        model.nudgeEnd(byMs: RangeSelectionModel.nudgeStepMs)  // +60 s
        XCTAssertEqual(model.range?.startMs, 100_000)
        XCTAssertEqual(model.range?.endMs, 360_000)
        XCTAssertEqual(model.phase, .menu, "the anchored menu stays put during fine-tune")
    }

    func testNudgeStartMovesTheEarlierEndpointEvenWhenMarkedSecond() {
        var model = RangeSelectionModel()
        model.enterSelectMode()
        model.mark(300_000)   // marked first, but it's the ordered END
        model.mark(100_000)   // ordered START
        model.nudgeStart(byMs: -RangeSelectionModel.nudgeStepMs)  // −60 s on the start
        XCTAssertEqual(model.range?.startMs, 40_000)
        XCTAssertEqual(model.range?.endMs, 300_000)
    }

    func testNudgeCollapsingEndpointsDropsRangeWithoutSilentReset() {
        var model = RangeSelectionModel()
        model.enterSelectMode()
        model.mark(100_000)
        model.mark(160_000)  // 60 s apart
        model.nudgeEnd(byMs: -RangeSelectionModel.nudgeStepMs)  // end → 100_000, coincides
        XCTAssertNil(model.range, "coincident endpoints are not a valid span")
        // Both marks are kept (never a silent full reset) — the mode is still
        // active so the user can nudge back out.
        XCTAssertTrue(model.isActive)
        XCTAssertEqual(model.phase, .selecting)
    }

    func testNudgeIgnoredWithoutBothEndpoints() {
        var model = RangeSelectionModel()
        model.enterSelectMode()
        model.mark(100_000)  // lone endpoint
        model.nudgeEnd(byMs: RangeSelectionModel.nudgeStepMs)
        XCTAssertEqual(model.pendingEndpointMs, 100_000, "nudge is a no-op with one endpoint")
    }

    // MARK: - Preselected range opens the menu (req 6)

    func testPreselectOpensMenuWithOrderedRange() {
        var model = RangeSelectionModel()
        model.preselect(startMs: 8_000, endMs: 2_000)  // supplied out of order
        XCTAssertEqual(model.phase, .menu)
        XCTAssertTrue(model.menuAnchored)
        XCTAssertEqual(model.range?.startMs, 2_000)
        XCTAssertEqual(model.range?.endMs, 8_000)
    }

    func testPreselectIgnoresZeroLengthRange() {
        var model = RangeSelectionModel()
        model.preselect(startMs: 5_000, endMs: 5_000)
        XCTAssertEqual(model.phase, .idle, "a zero-length preselection has nothing to act on")
        XCTAssertNil(model.range)
    }

    func testPreselectThenCancelClears() {
        var model = RangeSelectionModel()
        model.preselect(startMs: 2_000, endMs: 8_000)
        model.cancel()
        XCTAssertEqual(model.phase, .idle)
        XCTAssertNil(model.range)
    }

    // MARK: - Accessibility labels (R19 — the in-flight selection is perceivable)

    func testPendingSelectionLabelNamesBothEndpoints() {
        let label = DayStripAccessibility.pendingSelectionLabel(startMs: 0, endMs: 3_600_000)
        XCTAssertTrue(label.contains("Range selection"))
        XCTAssertFalse(label.isEmpty)
    }

    func testPendingEndpointLabelIsNonEmpty() {
        let label = DayStripAccessibility.pendingEndpointLabel(ms: 0)
        XCTAssertTrue(label.contains("Range start"))
        XCTAssertFalse(label.isEmpty)
    }
}
