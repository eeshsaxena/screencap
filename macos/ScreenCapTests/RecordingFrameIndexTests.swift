import Foundation
import XCTest
@testable import ScreenCap

// SCR-177 U1 — pure nearest-frame selection, the daemon-name path-containment
// guard, the `{epoch}.jpg` listing/parse, and the `resolve` staleness/nil-anchor
// behavior, all against a temp fixture root (no real recordings dir).
final class RecordingFrameIndexTests: XCTestCase {
    private var root: URL!

    override func setUpWithError() throws {
        root = FileManager.default.temporaryDirectory
            .appendingPathComponent("scr177-frames-\(UUID().uuidString.prefix(8))", isDirectory: true)
        try FileManager.default.createDirectory(at: root, withIntermediateDirectories: true)
    }

    override func tearDownWithError() throws {
        try? FileManager.default.removeItem(at: root)
    }

    /// Create `<root>/<recording>/screenshots/` and write empty frame files named
    /// by epoch seconds (recorder convention: `{epoch:.6f}.jpg`).
    @discardableResult
    private func makeFrames(_ recording: String, epochs: [Double], extra: [String] = []) throws -> URL {
        let dir = root
            .appendingPathComponent(recording, isDirectory: true)
            .appendingPathComponent("screenshots", isDirectory: true)
        try FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
        for e in epochs {
            try Data().write(to: dir.appendingPathComponent(String(format: "%.6f.jpg", e)))
        }
        for name in extra {
            try Data().write(to: dir.appendingPathComponent(name))
        }
        return dir
    }

    // MARK: - FrameSelection (pure)

    func testNearestPicksClosestByAbsoluteDistance() {
        let frames = [
            FrameRef(url: URL(fileURLWithPath: "/a"), ms: 1000),
            FrameRef(url: URL(fileURLWithPath: "/b"), ms: 5000),
            FrameRef(url: URL(fileURLWithPath: "/c"), ms: 9000),
        ]
        XCTAssertEqual(FrameSelection.nearest(toMs: 1000, in: frames)?.ms, 1000)   // exact
        XCTAssertEqual(FrameSelection.nearest(toMs: 6000, in: frames)?.ms, 5000)   // between -> nearer
        XCTAssertEqual(FrameSelection.nearest(toMs: -100, in: frames)?.ms, 1000)   // before first
        XCTAssertEqual(FrameSelection.nearest(toMs: 99_999, in: frames)?.ms, 9000) // after last
    }

    func testNearestEmptyIsNil() {
        XCTAssertNil(FrameSelection.nearest(toMs: 1000, in: []))
    }

    // MARK: - screenshotsDir containment (security)

    func testScreenshotsDirAcceptsSafeNameUnderRoot() {
        let dir = RecordingFrameIndex.screenshotsDir(root: root, recording: "rec-1")
        XCTAssertNotNil(dir)
        XCTAssertTrue(dir!.path.hasSuffix("/rec-1/screenshots"))
        XCTAssertTrue(dir!.path.hasPrefix(root.standardizedFileURL.path))
    }

    func testScreenshotsDirRejectsTraversalAndUnsafeNames() {
        XCTAssertNil(RecordingFrameIndex.screenshotsDir(root: root, recording: "../../Library/Keychains"))
        XCTAssertNil(RecordingFrameIndex.screenshotsDir(root: root, recording: ".."))
        XCTAssertNil(RecordingFrameIndex.screenshotsDir(root: root, recording: "a/b"))
        XCTAssertNil(RecordingFrameIndex.screenshotsDir(root: root, recording: ""))
    }

    // MARK: - loadFrames

    func testLoadFramesParsesSortsAndSkipsNonFrames() throws {
        try makeFrames(
            "rec",
            epochs: [1_719_400_002.5, 1_719_400_000.0, 1_719_400_001.25],
            extra: ["notanumber.jpg", "1719400000.000000.png"]
        )
        let frames = RecordingFrameIndex.loadFrames(root: root, recording: "rec")
        // Sorted ascending; .png and non-numeric stems skipped.
        XCTAssertEqual(frames.map(\.ms), [1_719_400_000_000, 1_719_400_001_250, 1_719_400_002_500])
    }

    func testLoadFramesMissingDirIsEmpty() {
        XCTAssertEqual(RecordingFrameIndex.loadFrames(root: root, recording: "nope"), [])
    }

    // MARK: - resolve (actor)

    func testResolveNilAnchorIsNilNeverFrameZero() async throws {
        try makeFrames("rec", epochs: [1_719_400_000.0])
        let index = RecordingFrameIndex(recordingsRoot: root)
        let url = await index.resolve(recording: "rec", anchorMs: nil)
        XCTAssertNil(url)
    }

    func testResolveWithinCapReturnsNearest() async throws {
        try makeFrames("rec", epochs: [1_719_400_000.0, 1_719_400_010.0])
        let index = RecordingFrameIndex(recordingsRoot: root, stalenessCapMs: 30_000)
        let url = await index.resolve(recording: "rec", anchorMs: 1_719_400_011_000) // 1s from 2nd frame
        XCTAssertEqual(url?.deletingPathExtension().lastPathComponent, "1719400010.000000")
    }

    func testResolveOverCapIsNil() async throws {
        try makeFrames("rec", epochs: [1_719_400_000.0])
        let index = RecordingFrameIndex(recordingsRoot: root, stalenessCapMs: 30_000)
        let url = await index.resolve(recording: "rec", anchorMs: 1_719_400_000_000 + 120_000) // 2 min away
        XCTAssertNil(url)
    }

    func testResolveTraversalNameIsNil() async throws {
        let index = RecordingFrameIndex(recordingsRoot: root)
        let url = await index.resolve(recording: "../../etc", anchorMs: 1_719_400_000_000)
        XCTAssertNil(url)
    }

    func testResolveIsConsistentAcrossCalls() async throws {
        try makeFrames("rec", epochs: [1_719_400_000.0])
        let index = RecordingFrameIndex(recordingsRoot: root)
        let a = await index.resolve(recording: "rec", anchorMs: 1_719_400_000_000)
        let b = await index.resolve(recording: "rec", anchorMs: 1_719_400_000_000)
        XCTAssertNotNil(a)
        XCTAssertEqual(a, b)
    }
}
