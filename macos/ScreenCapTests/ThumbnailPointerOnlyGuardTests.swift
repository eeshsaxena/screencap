import Foundation
import XCTest
@testable import ScreenCap

// SCR-177 U5 — pin the invariants the thumbnail feature must not break:
//   R8: the daemon wire models stay pointer-only (no media bytes/paths). A decode
//       round-trip alone CANNOT catch a regression here — Swift `Decodable`
//       silently drops unknown JSON keys, so a daemon that started emitting
//       `image_path` would decode cleanly into the unchanged model. The real
//       guard reflects over the declared properties.
//   R6: thumbnails are sourced only from the local recordings `screenshots/` dir.
//
// Lives in the default test target so CI runs it on every build (no opt-in
// marker) — confirm the macOS XCTest suite runs on every PR, not only releases.
final class ThumbnailPointerOnlyGuardTests: XCTestCase {

    /// Substrings that would indicate a media/path-carrying field. None of the
    /// pointer-only fields (recording, timestampMs, snippet, score, chunkIndex,
    /// app, title) contain any of these.
    private let mediaTokens = ["image", "photo", "thumbnail", "screenshot", "path", "url", "file", "bytes", "png", "jpeg", "jpg"]

    private func assertNoMediaProperty(
        _ value: Any, _ model: String, file: StaticString = #filePath, line: UInt = #line
    ) {
        for child in Mirror(reflecting: value).children {
            let label = (child.label ?? "").lowercased()
            for token in mediaTokens {
                XCTAssertFalse(
                    label.contains(token),
                    "\(model) has media/path-shaped field '\(label)' — R8 pointer-only regression",
                    file: file, line: line
                )
            }
        }
    }

    // MARK: - R8: wire models are pointer-only

    func testWireModelsExposeNoMediaProperty() throws {
        let content = try JSONDecoder().decode(
            ContentHit.self,
            from: Data(#"{"recording":"r","timestamp_ms":1,"snippet":"s","score":0.0}"#.utf8)
        )
        let transcript = try JSONDecoder().decode(
            TranscriptHit.self,
            from: Data(#"{"recording":"r","chunk_index":0,"snippet":"s"}"#.utf8)
        )
        let timeline = try JSONDecoder().decode(
            TimelineRow.self,
            from: Data(#"{"recording":"r","timestamp_ms":1,"app":"A","title":"T"}"#.utf8)
        )
        assertNoMediaProperty(content, "ContentHit")
        assertNoMediaProperty(transcript, "TranscriptHit")
        assertNoMediaProperty(timeline, "TimelineRow")
    }

    func testInjectedMediaKeysDoNotSurfaceOnModel() throws {
        // A media-bearing payload (as a future leaky daemon might emit) decodes
        // cleanly, and the injected keys do not surface on the pointer-only model.
        let json = #"""
        {"recording":"r","timestamp_ms":1000,"snippet":"s","score":-1.0,"image_path":"screenshots/1.jpg","png_data":"AAAA"}
        """#
        let hit = try JSONDecoder().decode(ContentHit.self, from: Data(json.utf8))
        XCTAssertEqual(hit.recording, "r")
        XCTAssertEqual(hit.timestampMs, 1000)
        XCTAssertEqual(hit.snippet, "s")
        assertNoMediaProperty(hit, "ContentHit (post-injection)")
    }

    // MARK: - R6: thumbnail source is the local screenshots dir only

    func testThumbnailSourceIsRootedUnderLocalScreenshots() {
        let root = URL(fileURLWithPath: "/Users/x/.screencap/recordings")
        let dir = RecordingFrameIndex.screenshotsDir(root: root, recording: "rec-1")
        XCTAssertEqual(dir?.path, "/Users/x/.screencap/recordings/rec-1/screenshots")
        // Never reads outside the recordings tree.
        XCTAssertNil(RecordingFrameIndex.screenshotsDir(root: root, recording: "../../Library/Keychains"))
    }

    /// The poster-frame fallback reads a video chunk from the recording dir, so
    /// that dir resolver carries the same R6 containment invariant as
    /// `screenshotsDir` — it must never resolve outside the recordings tree.
    func testPosterVideoSourceIsRootedUnderRecordingDir() {
        let root = URL(fileURLWithPath: "/Users/x/.screencap/recordings")
        let dir = RecordingFrameIndex.recordingDir(root: root, recording: "rec-1")
        XCTAssertEqual(dir?.path, "/Users/x/.screencap/recordings/rec-1")
        // Escapes and non-single-component names are rejected before any disk read.
        XCTAssertNil(RecordingFrameIndex.recordingDir(root: root, recording: "../../Library/Keychains"))
        XCTAssertNil(RecordingFrameIndex.recordingDir(root: root, recording: "a/b"))
        XCTAssertNil(RecordingFrameIndex.recordingDir(root: root, recording: ".."))
    }
}
