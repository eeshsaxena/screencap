import SQLite3
import XCTest
@testable import ScreenCap

/// U9 — the day-seek core (KTD-12) over fake manifests: day-time →
/// (chunk, PTS-anchored offset) resolution, the placeholder states, and the
/// disk loader over fixture recording dirs. No AVFoundation involved.
final class DayPlaybackEngineTests: XCTestCase {

    // MARK: - Manifest decode

    func testManifestDecodeToleratesExtraFieldsAndConvertsToMs() {
        let json = """
        {"format_version": 2, "chunk_index": 3, "chunk_start": 1700000000.25,
         "chunk_end": 1700000300.0, "stats": {"total_events": 4},
         "blocked_intervals": [{"start_ms": 1, "end_ms": 2}]}
        """
        let manifest = DayChunkManifest.decode(Data(json.utf8))
        XCTAssertEqual(manifest, DayChunkManifest(index: 3, startMs: 1_700_000_000_250, endMs: 1_700_000_300_000))
    }

    func testManifestDecodeReturnsNilOnMissingKeys() {
        XCTAssertNil(DayChunkManifest.decode(Data("{\"chunk_index\": 1}".utf8)))
        XCTAssertNil(DayChunkManifest.decode(Data("not json".utf8)))
    }

    // MARK: - Seek resolution (fake manifests)

    private let url = URL(fileURLWithPath: "/fake/chunk_0000.mp4")

    private func chunk(
        _ recording: String = "rec-a",
        start: Int, end: Int, anchor: Int?, hasMedia: Bool = true
    ) -> DayPlayableChunk {
        DayPlayableChunk(
            recording: recording,
            fileURL: hasMedia ? url : nil,
            startMs: start,
            endMs: end,
            anchorMs: anchor
        )
    }

    /// Chunk windows are half-open: a seek at an exact boundary lands in the
    /// *next* chunk at offset 0.
    func testSeekAtChunkBoundaryLandsInNextChunkAtOffsetZero() {
        let chunks = [
            chunk(start: 0, end: 60_000, anchor: 0),
            chunk(start: 60_000, end: 120_000, anchor: 60_000),
        ]
        let target = DayMediaMap.target(atMs: 60_000, in: chunks)
        XCTAssertEqual(target, .media(chunk: chunks[1], offsetSeconds: 0))
    }

    /// PTS is anchored at the chunk's first written frame, not `chunk_start` —
    /// an idle-starting chunk must not add the leading idle stretch to the
    /// offset (KTD-12).
    func testSeekIntoIdleStartingChunkAnchorsAtFirstWrittenFrame() {
        let idleStart = chunk(start: 0, end: 60_000, anchor: 10_000)
        guard case .media(_, let offset) = DayMediaMap.target(atMs: 25_000, in: [idleStart]) else {
            return XCTFail("expected media target")
        }
        XCTAssertEqual(offset, 15.0, accuracy: 0.001, "offset = t − first frame, not t − chunk_start")
        // Inside the leading idle stretch (before the first frame) → clamp to 0.
        guard case .media(_, let clamped) = DayMediaMap.target(atMs: 5_000, in: [idleStart]) else {
            return XCTFail("expected media target")
        }
        XCTAssertEqual(clamped, 0)
    }

    func testFramelessChunkResolvesToPlaceholder() {
        let frameless = chunk(start: 0, end: 60_000, anchor: nil)
        XCTAssertEqual(
            DayMediaMap.target(atMs: 30_000, in: [frameless]),
            .placeholder(.nothingCaptured)
        )
    }

    func testEvictedMediaResolvesToPlaceholderWithoutError() {
        let evicted = chunk(start: 0, end: 60_000, anchor: 0, hasMedia: false)
        XCTAssertEqual(
            DayMediaMap.target(atMs: 30_000, in: [evicted]),
            .placeholder(.mediaUnavailable)
        )
    }

    func testInterRecordingGapResolvesToPlaceholder() {
        let chunks = [
            chunk("rec-a", start: 0, end: 60_000, anchor: 0),
            chunk("rec-b", start: 300_000, end: 360_000, anchor: 300_000),
        ]
        XCTAssertEqual(
            DayMediaMap.target(atMs: 120_000, in: chunks),
            .placeholder(.nothingCaptured)
        )
    }

    func testLegacySingleVideoSeeksAsOneChunkAnchoredAtVideoStart() {
        let legacy = DayMediaMap.legacyChunk(
            recording: "rec-legacy",
            videoURL: url,
            videoStartMs: 1_000_000,
            durationMs: 600_000
        )
        XCTAssertNotNil(legacy)
        guard case .media(let resolved, let offset) =
                DayMediaMap.target(atMs: 1_030_000, in: [legacy!]) else {
            return XCTFail("expected media target")
        }
        XCTAssertEqual(resolved.recording, "rec-legacy")
        XCTAssertEqual(offset, 30.0, accuracy: 0.001)
    }

    func testNextChunkSkipsUnplayableChunks() {
        let chunks = [
            chunk(start: 0, end: 60_000, anchor: 0),
            chunk(start: 60_000, end: 120_000, anchor: nil),                    // frame-less
            chunk(start: 120_000, end: 180_000, anchor: 120_000, hasMedia: false),  // evicted
            chunk("rec-b", start: 180_000, end: 240_000, anchor: 181_000),
        ]
        XCTAssertEqual(DayMediaMap.nextChunk(afterMs: 60_000, in: chunks), chunks[3])
        XCTAssertNil(DayMediaMap.nextChunk(afterMs: 240_000, in: chunks))
    }

    // MARK: - Disk loader (fixture dirs)

    private func makeRecordingDir(_ root: URL, name: String) throws -> URL {
        let dir = root.appendingPathComponent(name, isDirectory: true)
        try FileManager.default.createDirectory(
            at: dir.appendingPathComponent("screenshots"), withIntermediateDirectories: true
        )
        return dir
    }

    private func write(_ dir: URL, _ filename: String, _ contents: String = "") throws {
        try Data(contents.utf8).write(to: dir.appendingPathComponent(filename))
    }

    func testLoadChunksBuildsFromManifestsFramesAndMediaPresence() throws {
        let root = FileManager.default.temporaryDirectory
            .appendingPathComponent("day-loader-\(UUID().uuidString)")
        defer { try? FileManager.default.removeItem(at: root) }
        let dir = try makeRecordingDir(root, name: "rec-a")

        // Chunk 0: media present, first frame 10s into the chunk (idle start).
        try write(dir, "chunk_0000_manifest.json",
                  "{\"chunk_index\": 0, \"chunk_start\": 1000.0, \"chunk_end\": 1300.0}")
        try write(dir, "chunk_0000.mp4")
        // Chunk 1: media evicted (manifest only).
        try write(dir, "chunk_0001_manifest.json",
                  "{\"chunk_index\": 1, \"chunk_start\": 1300.0, \"chunk_end\": 1600.0}")
        // Unparseable manifest is skipped, not fatal.
        try write(dir, "chunk_0002_manifest.json", "{\"chunk_index\": 2}")
        // Frames: one in chunk 0 (at t=1010s), none in chunk 1.
        try write(dir.appendingPathComponent("screenshots"), "1010.000000.jpg")

        let chunks = DayMediaLoader.loadChunks(
            root: root, recording: "rec-a", startedAtMs: nil, durationMs: nil
        )
        XCTAssertEqual(chunks.count, 2)
        XCTAssertEqual(chunks[0].startMs, 1_000_000)
        XCTAssertEqual(chunks[0].anchorMs, 1_010_000, "anchor = first frame in the chunk window")
        XCTAssertNotNil(chunks[0].fileURL)
        XCTAssertNil(chunks[1].fileURL, "evicted chunk keeps its span with no media")
        XCTAssertNil(chunks[1].anchorMs)
    }

    /// Recordings without the flat `screenshots/*.jpg` export still anchor:
    /// the loader falls back to the `screenshot` table timestamps (KTD-12's
    /// "frame index/recording.db").
    func testLoadChunksAnchorsFromRecordingDBWhenScreenshotsDirIsEmpty() throws {
        let root = FileManager.default.temporaryDirectory
            .appendingPathComponent("day-loader-\(UUID().uuidString)")
        defer { try? FileManager.default.removeItem(at: root) }
        let dir = try makeRecordingDir(root, name: "rec-a")
        try write(dir, "chunk_0000_manifest.json",
                  "{\"chunk_index\": 0, \"chunk_start\": 1000.0, \"chunk_end\": 1300.0}")
        try write(dir, "chunk_0000.mp4")
        try makeRecordingDB(
            at: dir.appendingPathComponent("recording.db"),
            timestamp: 990.0, videoStartTime: nil,
            screenshotTimestamps: [1012.5, 1100.0]
        )

        let chunks = DayMediaLoader.loadChunks(
            root: root, recording: "rec-a", startedAtMs: nil, durationMs: nil
        )
        XCTAssertEqual(chunks.count, 1)
        XCTAssertEqual(chunks[0].anchorMs, 1_012_500, "first screenshot-table frame in the chunk window")
    }

    func testLoadChunksLegacyRecordingAnchorsAtRecordingDBVideoStart() throws {
        let root = FileManager.default.temporaryDirectory
            .appendingPathComponent("day-loader-\(UUID().uuidString)")
        defer { try? FileManager.default.removeItem(at: root) }
        let dir = try makeRecordingDir(root, name: "rec-legacy")
        try write(dir, "video.mp4")
        try makeRecordingDB(
            at: dir.appendingPathComponent("recording.db"),
            timestamp: 2000.0, videoStartTime: 2005.5
        )

        let chunks = DayMediaLoader.loadChunks(
            root: root, recording: "rec-legacy",
            startedAtMs: 2_000_000, durationMs: 300_000
        )
        XCTAssertEqual(chunks.count, 1)
        XCTAssertEqual(chunks[0].anchorMs, 2_005_500, "video_start_time wins over started_at")
        XCTAssertEqual(chunks[0].startMs, 2_005_500)
        XCTAssertEqual(chunks[0].endMs, 2_305_500)
        XCTAssertNotNil(chunks[0].fileURL)
    }

    func testLoadChunksLegacyFallsBackToStartedAtWhenDBUnreadable() throws {
        let root = FileManager.default.temporaryDirectory
            .appendingPathComponent("day-loader-\(UUID().uuidString)")
        defer { try? FileManager.default.removeItem(at: root) }
        let dir = try makeRecordingDir(root, name: "rec-legacy")
        try write(dir, "video.mp4")
        try write(dir, "recording.db", "not a database")

        let chunks = DayMediaLoader.loadChunks(
            root: root, recording: "rec-legacy",
            startedAtMs: 2_000_000, durationMs: 300_000
        )
        XCTAssertEqual(chunks.count, 1)
        XCTAssertEqual(chunks[0].anchorMs, 2_000_000)
    }

    func testLoadChunksRejectsPathEscapingNames() {
        XCTAssertEqual(
            DayMediaLoader.loadChunks(
                root: FileManager.default.temporaryDirectory,
                recording: "../escape", startedAtMs: nil, durationMs: nil
            ),
            []
        )
    }

    // MARK: - SQLite fixture

    private func makeRecordingDB(
        at url: URL,
        timestamp: Double,
        videoStartTime: Double?,
        screenshotTimestamps: [Double] = []
    ) throws {
        var db: OpaquePointer?
        XCTAssertEqual(sqlite3_open(url.path, &db), SQLITE_OK)
        defer { sqlite3_close(db) }
        let create = """
        CREATE TABLE recording (timestamp REAL, video_start_time REAL);
        CREATE TABLE screenshot (timestamp REAL);
        """
        XCTAssertEqual(sqlite3_exec(db, create, nil, nil, nil), SQLITE_OK)
        let vst = videoStartTime.map { String($0) } ?? "NULL"
        let insert = "INSERT INTO recording VALUES (\(timestamp), \(vst))"
        XCTAssertEqual(sqlite3_exec(db, insert, nil, nil, nil), SQLITE_OK)
        for ts in screenshotTimestamps {
            XCTAssertEqual(sqlite3_exec(db, "INSERT INTO screenshot VALUES (\(ts))", nil, nil, nil), SQLITE_OK)
        }
    }
}
