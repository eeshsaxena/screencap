import Foundation
import XCTest
@testable import Screencap

/// U1 — the task-span thumbnail's chunk/offset resolution (KTD-4). Pure over a
/// prebuilt chunk list: the covering chunk is chosen and the extraction offset is
/// anchored at the chunk's first-written-frame `anchorMs`, not the manifest
/// `chunk_start`, so an action-gated idle lead-in never mis-seeks to a black frame.
final class TaskThumbnailResolutionTests: XCTestCase {
    private let url = URL(fileURLWithPath: "/fake/chunk_0000.mp4")

    private func chunk(start: Int, end: Int, anchor: Int?, hasMedia: Bool = true) -> DayPlayableChunk {
        DayPlayableChunk(
            recording: "rec-a",
            fileURL: hasMedia ? url : nil,
            startMs: start,
            endMs: end,
            anchorMs: anchor
        )
    }

    /// A task starting 30s into the second (900s) chunk resolves to that chunk's
    /// mp4 at a 30s offset.
    func testPosterTargetPicksCoveringChunkAndAnchorOffset() {
        let chunks = [
            chunk(start: 0, end: 900_000, anchor: 0),
            chunk(start: 900_000, end: 1_800_000, anchor: 900_000),
        ]
        let resolved = RecordingFrameIndex.posterTarget(taskStartMs: 930_000, in: chunks)
        XCTAssertEqual(resolved?.url, url)
        XCTAssertEqual(resolved?.offsetSeconds ?? -1, 30, accuracy: 0.001)
    }

    /// The offset is measured from the chunk's first written frame (F1/KTD-12):
    /// a chunk that begins idle at 0 with its first frame 10s in, seeked at 25s,
    /// extracts at 15s — not 25s, which would land in the black idle lead-in.
    func testPosterTargetAnchorsAtFirstFrameNotChunkStart() {
        let chunks = [chunk(start: 0, end: 60_000, anchor: 10_000)]
        let resolved = RecordingFrameIndex.posterTarget(taskStartMs: 25_000, in: chunks)
        XCTAssertEqual(resolved?.offsetSeconds ?? -1, 15, accuracy: 0.001)
    }

    /// A task instant inside the leading idle stretch (before the first frame)
    /// clamps to offset 0 rather than seeking negative.
    func testPosterTargetClampsIdleLeadInToZero() {
        let chunks = [chunk(start: 0, end: 60_000, anchor: 10_000)]
        let resolved = RecordingFrameIndex.posterTarget(taskStartMs: 5_000, in: chunks)
        XCTAssertEqual(resolved?.offsetSeconds ?? -1, 0, accuracy: 0.001)
    }

    /// A task offset outside every chunk yields nil, so the caller falls through
    /// to the recording poster / placeholder.
    func testPosterTargetNilWhenOutsideEveryChunk() {
        let chunks = [chunk(start: 0, end: 60_000, anchor: 0)]
        XCTAssertNil(RecordingFrameIndex.posterTarget(taskStartMs: 120_000, in: chunks))
    }

    /// A covering chunk whose local media was evicted after upload yields nil
    /// (never a false frame).
    func testPosterTargetNilWhenCoveringChunkMediaEvicted() {
        let chunks = [chunk(start: 0, end: 60_000, anchor: 0, hasMedia: false)]
        XCTAssertNil(RecordingFrameIndex.posterTarget(taskStartMs: 30_000, in: chunks))
    }
}
