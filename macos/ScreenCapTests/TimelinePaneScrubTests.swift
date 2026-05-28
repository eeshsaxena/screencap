import XCTest
@testable import ScreenCap

/// U6 scrub-math tests. The Canvas / DragGesture pipeline is exercised
/// indirectly: the production code routes drag X coordinates through
/// `TimelinePaneScrub.scrubSeconds` and the cursor's drawn X through
/// `cursorX(forSeconds:...)`, so pinning those two pure functions covers
/// the timeline ↔ player sync contract without driving SwiftUI.
final class TimelinePaneScrubTests: XCTestCase {
    func testScrubAtMidpointMapsToHalfDuration() {
        let seconds = TimelinePaneScrub.scrubSeconds(
            forX: 150,
            width: 300,
            durationSeconds: 30
        )
        XCTAssertEqual(seconds, 15, accuracy: 0.001)
    }

    func testScrubAtComputedXMatchesExpectedTimestamp() {
        // Width 600, duration 30s. X=200 → 1/3 of the way → 10s.
        let seconds = TimelinePaneScrub.scrubSeconds(
            forX: 200,
            width: 600,
            durationSeconds: 30
        )
        XCTAssertEqual(seconds, 10, accuracy: 0.001)
    }

    func testScrubBeyondWidthClampsToDurationEnd() {
        let seconds = TimelinePaneScrub.scrubSeconds(
            forX: 999,
            width: 300,
            durationSeconds: 30
        )
        XCTAssertEqual(seconds, 30, accuracy: 0.001)
    }

    func testScrubBelowZeroClampsToStart() {
        let seconds = TimelinePaneScrub.scrubSeconds(
            forX: -50,
            width: 300,
            durationSeconds: 30
        )
        XCTAssertEqual(seconds, 0, accuracy: 0.001)
    }

    /// Very-short recording: drag math must still produce in-bounds values.
    func testShortRecordingDragStaysWithinBounds() {
        for x in stride(from: CGFloat(0), through: CGFloat(300), by: 50) {
            let seconds = TimelinePaneScrub.scrubSeconds(
                forX: x,
                width: 300,
                durationSeconds: 0.5
            )
            XCTAssertGreaterThanOrEqual(seconds, 0)
            XCTAssertLessThanOrEqual(seconds, 0.5)
        }
    }

    func testZeroWidthOrDurationReturnsZero() {
        XCTAssertEqual(
            TimelinePaneScrub.scrubSeconds(forX: 100, width: 0, durationSeconds: 30),
            0
        )
        XCTAssertEqual(
            TimelinePaneScrub.scrubSeconds(forX: 100, width: 300, durationSeconds: 0),
            0
        )
    }

    func testCursorXTracksExternalCurrentTimeUpdates() {
        let xAt5 = TimelinePaneScrub.cursorX(
            forSeconds: 5,
            width: 300,
            durationSeconds: 30
        )
        let xAt5_5 = TimelinePaneScrub.cursorX(
            forSeconds: 5.5,
            width: 300,
            durationSeconds: 30
        )
        XCTAssertEqual(xAt5, 50, accuracy: 0.001)
        XCTAssertEqual(xAt5_5, 55, accuracy: 0.001)
    }

    /// Per the plan: "external `currentTime` update from 5.0 → 5.5 → cursor
    /// X coord changes proportionally." This pins the ratio rather than
    /// absolute values, so a future timeline width change does not break.
    func testCursorXChangeIsProportionalToTimeChange() {
        let width: CGFloat = 1000
        let duration: Double = 60
        let x1 = TimelinePaneScrub.cursorX(forSeconds: 10, width: width, durationSeconds: duration)
        let x2 = TimelinePaneScrub.cursorX(forSeconds: 20, width: width, durationSeconds: duration)
        XCTAssertEqual(x2 - x1, width / 6, accuracy: 0.001)
    }

    /// Scrub-and-seek feedback loop: if a drag generates a scrub timestamp
    /// equal to the current player time, the round-trip cursor X coordinate
    /// must match (no off-by-one drift each cycle). Anchors the
    /// timeline-to-player vs player-to-timeline math against each other.
    func testScrubAndCursorAreInverseFunctions() {
        let width: CGFloat = 400
        let duration: Double = 120
        for x in [CGFloat(0), 50, 100, 200, 350, 400] {
            let seconds = TimelinePaneScrub.scrubSeconds(
                forX: x,
                width: width,
                durationSeconds: duration
            )
            let backToX = TimelinePaneScrub.cursorX(
                forSeconds: seconds,
                width: width,
                durationSeconds: duration
            )
            XCTAssertEqual(backToX, x, accuracy: 0.001)
        }
    }
}
