import XCTest
@testable import ScreenCap

/// U6 screenshot truth-view selection tests. The masked-frame mapping is the
/// pane's only non-trivial logic; pinning the pure `ScreenshotTruth` helpers
/// covers the timeline ↔ frame sync without a SwiftUI render (mirrors
/// `TimelinePaneScrubTests`).
final class ScreenshotTruthPaneTests: XCTestCase {
    private let startedAt: Double = 1_700_000_000.0

    private func url(_ ts: Double) -> URL {
        URL(fileURLWithPath: "/tmp/rec-scrubbed/screenshots/\(String(format: "%.6f", ts)).jpg")
    }

    func testScreenshotsParseRelativeTimesAndSort() {
        let urls = [url(startedAt + 10), url(startedAt + 2), url(startedAt + 5)]
        let shots = ScreenshotTruth.screenshots(from: urls, startedAt: startedAt)
        XCTAssertEqual(shots.map(\.relativeSeconds), [2, 5, 10])
    }

    func testNonNumericFilenameIsSkipped() {
        let urls = [
            url(startedAt + 1),
            URL(fileURLWithPath: "/tmp/rec-scrubbed/screenshots/thumbnail.jpg"),
        ]
        let shots = ScreenshotTruth.screenshots(from: urls, startedAt: startedAt)
        XCTAssertEqual(shots.count, 1)
        XCTAssertEqual(shots.first?.relativeSeconds, 1)
    }

    func testExactFrameTimeSelectsThatFrameWithZeroOffset() {
        let shots = ScreenshotTruth.screenshots(
            from: [url(startedAt + 5), url(startedAt + 10)], startedAt: startedAt)
        let sel = ScreenshotTruth.selection(at: 5, screenshots: shots)
        guard case .frame(let u, let offset) = sel else { return XCTFail("expected frame, got \(sel)") }
        XCTAssertEqual(u, url(startedAt + 5))
        XCTAssertEqual(offset, 0, accuracy: 0.001)
    }

    func testBetweenFramesSelectsNearestPriorWithStalenessOffset() {
        let shots = ScreenshotTruth.screenshots(
            from: [url(startedAt + 2), url(startedAt + 14)], startedAt: startedAt)
        // At t=26, the 14s frame is the nearest prior → 12s stale.
        let sel = ScreenshotTruth.selection(at: 26, screenshots: shots)
        guard case .frame(let u, let offset) = sel else { return XCTFail("expected frame, got \(sel)") }
        XCTAssertEqual(u, url(startedAt + 14))
        XCTAssertEqual(offset, 12, accuracy: 0.001)
    }

    func testBeforeFirstFrameRendersBoundaryNotTheOriginal() {
        let shots = ScreenshotTruth.screenshots(from: [url(startedAt + 5)], startedAt: startedAt)
        let sel = ScreenshotTruth.selection(at: 1, screenshots: shots)
        XCTAssertEqual(sel, .beforeFirst, "must NOT fall back to an unmasked original frame")
    }

    func testAfterLastFrameSelectsLastWithGrowingOffset() {
        let shots = ScreenshotTruth.screenshots(from: [url(startedAt + 3)], startedAt: startedAt)
        let sel = ScreenshotTruth.selection(at: 100, screenshots: shots)
        guard case .frame(let u, let offset) = sel else { return XCTFail("expected frame, got \(sel)") }
        XCTAssertEqual(u, url(startedAt + 3))
        XCTAssertEqual(offset, 97, accuracy: 0.001)
    }

    func testEmptyScreenshotSetRendersEmptyNotCrash() {
        XCTAssertEqual(ScreenshotTruth.selection(at: 5, screenshots: []), .empty)
    }

    /// When started_at is unknown (review.py null → ViewModel falls back to 0),
    /// frames anchor to the earliest captured frame so they map onto the 0-based
    /// playback axis instead of all reading as .beforeFirst (which would make the
    /// "what uploads" surface wrongly appear empty).
    func testUnknownStartedAtAnchorsToEarliestFrame() {
        let shots = ScreenshotTruth.screenshots(
            from: [url(startedAt + 12), url(startedAt + 4)], startedAt: 0)
        XCTAssertEqual(shots.map(\.relativeSeconds), [0, 8], "anchored to the earliest frame")

        // Selecting at the later frame's time finds it — not .beforeFirst.
        if case .frame = ScreenshotTruth.selection(at: 8, screenshots: shots) {} else {
            XCTFail("expected a frame at t=8, not a boundary state")
        }
    }

    func testClockLabelFormatsMinutesSeconds() {
        XCTAssertEqual(ScreenshotTruth.clockLabel(0), "0:00")
        XCTAssertEqual(ScreenshotTruth.clockLabel(14), "0:14")
        XCTAssertEqual(ScreenshotTruth.clockLabel(75), "1:15")
    }
}
