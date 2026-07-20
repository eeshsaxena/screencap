import Foundation
import XCTest
@testable import Screencap

/// U2 — the task view's pure geometry (KTD-2, KTD-3): the task-scoped axis bounds
/// and the task-overlapping chunk selection, so the player scopes to the task's
/// own footage and the strip axis reads as the task, both without a `timeline.day`
/// call.
final class TaskDetailLayoutTests: XCTestCase {
    private let url = URL(fileURLWithPath: "/fake/chunk.mp4")

    private func chunk(start: Int, end: Int) -> DayPlayableChunk {
        DayPlayableChunk(recording: "rec-a", fileURL: url, startMs: start, endMs: end, anchorMs: start)
    }

    func testBoundsCoverTaskSpanWithPadding() {
        let b = TaskSpanLayout.bounds(startMs: 1_000_000, endMs: 1_060_000)
        XCTAssertEqual(b.startMs, 1_000_000 - TaskSpanLayout.padMs)
        XCTAssertEqual(b.endMs, 1_060_000 + TaskSpanLayout.padMs)
        XCTAssertGreaterThan(b.spanMs, 0)
    }

    /// A live task passes endMs = now; the axis end follows now (+ padding) so the
    /// growing tail is visible.
    func testBoundsFollowNowForLiveTask() {
        let now = 1_100_000
        let b = TaskSpanLayout.bounds(startMs: 1_000_000, endMs: now)
        XCTAssertEqual(b.endMs, now + TaskSpanLayout.padMs)
    }

    /// A sub-second (or zero-length) task still yields a positive-span axis.
    func testBoundsPositiveSpanForInstantTask() {
        let b = TaskSpanLayout.bounds(startMs: 500_000, endMs: 500_000)
        XCTAssertGreaterThan(b.spanMs, 0)
    }

    /// A reversed span (endMs < startMs — malformed input) still yields a
    /// positive, correctly-ordered axis rather than an inverted one.
    func testBoundsPositiveSpanForReversedSpan() {
        let b = TaskSpanLayout.bounds(startMs: 600_000, endMs: 500_000)
        XCTAssertGreaterThan(b.spanMs, 0)
        XCTAssertLessThan(b.startMs, b.endMs)
    }

    func testOverlappingChunksKeepsOnlyIntersecting() {
        let chunks = [
            chunk(start: 0, end: 900_000),          // before the task — dropped
            chunk(start: 900_000, end: 1_800_000),  // overlaps the task — kept
            chunk(start: 1_800_000, end: 2_700_000),// after the task — dropped
        ]
        let selected = TaskSpanLayout.overlappingChunks(chunks, startMs: 1_000_000, endMs: 1_200_000)
        XCTAssertEqual(selected, [chunks[1]])
    }

    /// A task spanning a chunk boundary keeps both chunks it touches.
    func testOverlappingChunksKeepsBothAcrossBoundary() {
        let chunks = [
            chunk(start: 0, end: 900_000),
            chunk(start: 900_000, end: 1_800_000),
        ]
        let selected = TaskSpanLayout.overlappingChunks(chunks, startMs: 880_000, endMs: 920_000)
        XCTAssertEqual(selected, chunks)
    }
}
