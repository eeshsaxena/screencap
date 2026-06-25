import XCTest
@testable import ScreenCap

/// SCR-174 U6 — the absolute-ms → recording-relative-seconds conversion for the
/// search deep-link, including the null-origin guard and duration clamp. The
/// window/AVPlayer wiring itself is manual-QA (SwiftUI + openWindow are not
/// unit-testable here).
final class ReviewSeekTargetTests: XCTestCase {
    func testConvertsAnchorToRelativeSeconds() {
        // started at t=1_000_000s; hit 90s in; 300s recording.
        let anchorMs = (1_000_000 + 90) * 1000
        XCTAssertEqual(
            SearchSeek.relativeSeconds(anchorMs: anchorMs, startedAt: 1_000_000, durationSeconds: 300),
            90, accuracy: 0.001
        )
    }

    func testNullOrZeroStartFallsBackToZero() {
        // startedAt <= 0 (SCR-107 corrupt-timing path) must not produce a huge
        // seek — fall back to the start.
        XCTAssertEqual(
            SearchSeek.relativeSeconds(anchorMs: 1_700_000_000_000, startedAt: 0, durationSeconds: 300),
            0, accuracy: 0.001
        )
    }

    func testAnchorBeforeStartClampsToZero() {
        let anchorMs = (1_000_000 - 50) * 1000
        XCTAssertEqual(
            SearchSeek.relativeSeconds(anchorMs: anchorMs, startedAt: 1_000_000, durationSeconds: 300),
            0, accuracy: 0.001
        )
    }

    func testRelativeClampsToKnownDuration() {
        let anchorMs = (1_000_000 + 9_999) * 1000  // well past the end
        XCTAssertEqual(
            SearchSeek.relativeSeconds(anchorMs: anchorMs, startedAt: 1_000_000, durationSeconds: 300),
            300, accuracy: 0.001
        )
    }

    func testNoClampWhenDurationUnknown() {
        let anchorMs = (1_000_000 + 500) * 1000
        XCTAssertEqual(
            SearchSeek.relativeSeconds(anchorMs: anchorMs, startedAt: 1_000_000, durationSeconds: 0),
            500, accuracy: 0.001
        )
    }
}
